import os
import torch
from torch.utils.data import DataLoader, TensorDataset
import nvflare.client as flare

# In a real implementation, you might use boto3 and environment variables 
# to connect to your Minio instance.
# import boto3


def init_flare():
    """
    Initializes the FLARE client and prints a confirmation message.
    """
    flare.init()
    print("flare_adapter: NVIDIA FLARE client initialized.")

def get_data(dataset_path: str = None):
    """
    Gets the training data for the client.

    In a real-world scenario, this function would use the dataset_path
    to download the correct data from Minio/S3 storage.

    Args:
        dataset_path (str, optional): The path to the dataset in storage. Defaults to None.

    Returns:
        A PyTorch DataLoader.
    """
    # --- Placeholder for Minio/S3 data loading ---
    # For this example, we will generate dummy data. 
    # In your real code, you would replace this section.
    # Example of what it might look like:
    #
    # s3_client = boto3.client('s3', endpoint_url=os.getenv('AWS_S3_ENDPOINT_URL'))
    # project_id = os.getenv('FLARE_PROJECT_ID') # You would set this env var
    # file_key = f"{project_id}/data/{dataset_path}"
    # s3_client.download_file(os.getenv('AWS_STORAGE_BUCKET_NAME'), file_key, 'local_data.csv')
    # 
    # # Now load your local_data.csv into a DataLoader
    # print(f"flare_adapter: Successfully downloaded data from {dataset_path}")
    # --- End of placeholder ---

    print("flare_adapter: Generating dummy data for demonstration.")
    X_train = torch.randn(100, 10)
    y_train = torch.randn(100, 1)
    dataset = TensorDataset(X_train, y_train)
    train_loader = DataLoader(dataset, batch_size=10, shuffle=True)
    return train_loader

def receive_model(model: torch.nn.Module):
    """
    Receives the global model from the FLARE server and loads it into the local model.

    Args:
        model (torch.nn.Module): The local PyTorch model instance.
    """
    print("flare_adapter: Receiving global model from server...")
    input_model = flare.receive()
    model.load_state_dict(input_model.params)
    print("flare_adapter: Global model weights loaded.")
    return input_model # Return the model in case you need metadata

def send_model(model: torch.nn.Module, metrics: dict = None):
    """
    Packages the local model weights and metrics into an FLModel and sends it to the server.

    Args:
        model (torch.nn.Module): The trained local PyTorch model instance.
        metrics (dict, optional): A dictionary of metrics (e.g., {"loss": 0.5}). Defaults to None.
    """
    print("flare_adapter: Sending updated model to server...")
    output_model = flare.FLModel(
        params=model.state_dict(),
        metrics=metrics if metrics is not None else {},
    )
    flare.send(output_model)
    print("flare_adapter: Model sent.")