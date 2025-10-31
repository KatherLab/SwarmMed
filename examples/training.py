import torch
import torch.nn as nn
import torch.optim as optim

# Import the helper functions from your new adapter
# FLARE will add the app directory to the python path, so this import will work
from apps.training import flare_adapter

# 1. Define your model architecture (same as before)
   class SimpleNet(nn.Module):
       def __init__(self):
           super(SimpleNet, self).__init__()
           self.fc1 = nn.Linear(10, 5)
           self.relu = nn.ReLU()
           self.fc2 = nn.Linear(5, 1)
   
       def forward(self, x):
           x = self.fc1(x)
           x = self.relu(x)
           x = self.fc2(x)
           return x
   
   # 2. Main training logic
   def main():
       # Initialize the FLARE client using the adapter
       flare_adapter.init_flare()
   
       # Create the model, loss function, and optimizer
       model = SimpleNet()
       criterion = nn.MSELoss()
       optimizer = optim.SGD(model.parameters(), lr=0.01)
   
       # Get data using the adapter.
       # In the future, you could pass a path like:
       # train_loader = flare_adapter.get_data(dataset_path="my_data.csv")
       train_loader = flare_adapter.get_data()
       
       # The client's training loop runs as long as the FLARE server is running the job
       while True: # The loop will be controlled by flare.receive() which exits on job end
           # Receive the global model using the adapter
           flare_adapter.receive_model(model)
   
           # --- Your local training loop ---
           local_epochs = 3
           print(f"Starting local training for {local_epochs} epochs...")
           for epoch in range(local_epochs):
               running_loss = 0.0
               for i, data in enumerate(train_loader, 0):
                   inputs, labels = data
                   optimizer.zero_grad()
                   outputs = model(inputs)
                   loss = criterion(outputs, labels)
                   loss.backward()
                   optimizer.step()
                   running_loss += loss.item()
               
               print(f"  [Epoch {epoch + 1}] Loss: {running_loss / len(train_loader):.3f}")
           print("Local training finished.")
   
           # Send the updated model and metrics back to the server using the adapter
           flare_adapter.send_model(
               model=model, 
               metrics={"loss": running_loss / len(train_loader)}
           )
   
   if __name__ == "__main__":
       try:
           main()
       except Exception as e:
           # If flare.receive() determines the job is over, it raises an exception.
           # This is the expected way to exit the training loop.
           print(f"FLARE training loop finished or encountered an error: {e}")