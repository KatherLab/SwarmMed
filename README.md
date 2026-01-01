# SwarmCloud

## Installation

### 1. Docker

Install Docker and Docker Compose by following the official documentation for your platform:

https://docs.docker.com/engine/install/

### 2. VPN Network (Tailscale)

```bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
```
```bash
sudo apt update
sudo apt install tailscale
```
Login with credentials:
```bash
sudo tailscale up
```
Test connectivity:
```bash
tailscale ip -4 
```

### 3. SwarmCloud

```bash
git clone https://github.com/pfeifferis/SwarmCloud.git
```
```bash
docker compose build
docker compose up
```

### 4. Create Superuser
```bash
docker exec -it swarmcloud python manage.py createsuperuser
```


## During Development

```bash
source venv/bin/activate
deactivate
```
```bash
remove in production in docker-compose.yml:
volumes:
      - ./:/app
    ports:
      - "8000:8000"
command: python manage.py runserver 0.0.0.0:8000
```

```bash
change css files locally during development in vs code new terminal

npm i
npm run build
npx tailwindcss -i ./static/assets/style.css -o ./static/dist/css/output.css --watch
npx webpack --watch
```
### Troubleshooting
#### No Tailscale Connection
```bash
tailscale set --accept-dns=false
systemctl restart tailscaled
```


## Start Documentation website

```bash
mkdocs serve --dev-addr localhost:9999
mkdocs serve --dev-addr 0.0.0.0:9999
``` 

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details on how to get started, our coding standards (PEP 8), and the project architecture.

## when changing tasks
```bash
docker compose restart celery_worker

snyk code test --json > snyk_code_report.json
```

## security scans
```bash
snyk code test --json > snyk_code_report.json
snyk test --json > snyk_report.json
 ./venv/bin/python -m bandit -r apps core home manage.py -f json -o bandit_report_final.json    
safety scan
```
