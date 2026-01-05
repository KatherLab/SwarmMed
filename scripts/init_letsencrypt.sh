#!/bin/bash

# Usage: ./scripts/init_letsencrypt.sh [staging]
# Pass 'staging' as an argument to use the Let's Encrypt staging server (recommended for dev).

if ! [ -x "$(command -v docker-compose)" ]; then
  echo 'Error: docker-compose is not installed.' >&2
  exit 1
fi

domains=()
rsa_key_size=4096
data_path="./infrastructure/certbot"
email="" # Adding a valid address is strongly recommended
staging=0 # Set to 1 if you're testing your setup to avoid hitting request limits

# Check for staging flag in arguments
if [ "$1" == "staging" ]; then
  staging=1
  echo "### Using Let's Encrypt STAGING environment ###"
fi

read -p "Enter your domain name (e.g. dev.example.com): " domain
domains+=($domain)

if [ -z "$domain" ]; then
    echo "Domain name is required"
    exit 1
fi

read -p "Enter your email for Certbot updates: " email

if [ -d "$data_path/conf/live/$domain" ]; then
  read -p "Existing data found for $domain. Continue and replace existing certificate? (y/N) " decision
  if [ "$decision" != "Y" ] && [ "$decision" != "y" ]; then
    exit
  fi
fi


if [ ! -e "$data_path/conf/options-ssl-nginx.conf" ] || [ ! -e "$data_path/conf/ssl-dhparams.pem" ]; then
  echo "### Downloading recommended TLS parameters ..."
  mkdir -p "$data_path/conf"
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf > "$data_path/conf/options-ssl-nginx.conf"
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem > "$data_path/conf/ssl-dhparams.pem"
  echo
fi

echo "### Creating dummy certificate for $domains ..."
path="/etc/letsencrypt/live/$domains"
mkdir -p "$data_path/conf/live/$domains"
docker-compose run --rm --entrypoint \
  "openssl req -x509 -nodes -newkey rsa:$rsa_key_size -days 1    -keyout '$path/privkey.pem' \
    -out '$path/fullchain.pem' \
    -subj '/CN=localhost'" certbot
echo


echo "### Starting nginx ..."
docker-compose up --force-recreate -d nginx
echo

echo "### Deleting dummy certificate for $domains ..."
docker-compose run --rm --entrypoint \
  "rm -Rf /etc/letsencrypt/live/$domains && \
  rm -Rf /etc/letsencrypt/archive/$domains && \
  rm -Rf /etc/letsencrypt/renewal/$domains.conf" certbot
echo


echo "### Requesting Let's Encrypt certificate for $domains ..."
#Join $domains to -d args
domain_args=""
for domain in "${domains[@]}"; do
  domain_args="$domain_args -d $domain"
done

# Select appropriate email arg
case "$email" in
  "" ) # This is an empty string
    email_arg="--register-unsafely-without-email" 
    ;;
  *)
    email_arg="-m $email"
    ;;
esac

# Enable staging mode if needed
staging_arg=""
if [ $staging != "0" ]; then staging_arg="--staging"; fi

docker-compose run --rm --entrypoint \
  "certbot certonly --webroot -w /var/www/certbot \
    $staging_arg \
    $email_arg \
    $domain_args \
    --rsa-key-size $rsa_key_size \
    --agree-tos \
    --force-renewal" certbot
echo

echo "### Reloading nginx ..."
docker-compose exec nginx nginx -s reload

echo "### SUCCESS: Certificates obtained!"
echo "---------------------------------------------------------"
echo "NEXT STEPS:"
echo "1. Update nginx/app.conf to use the new certificate paths:"
echo "   ssl_certificate /etc/letsencrypt/live/$domain/fullchain.pem;"
echo "   ssl_certificate_key /etc/letsencrypt/live/$domain/privkey.pem;"
echo "2. Ensure env variables for DOMAIN_NAME are set if using variables."
echo "3. Restart nginx: docker-compose restart nginx"
