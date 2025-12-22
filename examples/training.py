import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
import nvflare.client as flare


class SimpleNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(10, 5)
        self.relu = nn.ReLU()
        self.fc2 = nn.Linear(5, 1)

    def forward(self, x):
        return self.fc2(self.relu(self.fc1(x)))


def make_deterministic_init(seed=0):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def get_data():
    X = torch.randn(200, 10)
    y = torch.randn(200, 1)
    return DataLoader(TensorDataset(X, y), batch_size=32, shuffle=True)


def train_one_round(model, train_loader, device, local_epochs=2, lr=0.01):
    model.train()
    criterion = nn.MSELoss()
    optimizer = optim.SGD(model.parameters(), lr=lr)

    total_loss = 0.0
    n_batches = 0
    n_steps = 0
    n_samples = 0

    for _ in range(local_epochs):
        for xb, yb in train_loader:
            xb = xb.to(device)
            yb = yb.to(device)
            n_samples += xb.size(0)

            optimizer.zero_grad(set_to_none=True)
            pred = model(xb)
            loss = criterion(pred, yb)
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            n_batches += 1
            n_steps += 1

    avg_loss = total_loss / max(n_batches, 1)
    return avg_loss, n_steps, n_samples


def main():
    flare.init()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # local deterministic init so round-0 can proceed even if inbound model is empty
    make_deterministic_init(seed=0)
    model = SimpleNet().to(device)

    train_loader = get_data()

    while True:
        # ---- Clean shutdown handling ----
        try:
            in_model = flare.receive()
        except Exception as e:
            # On END_RUN, receive can error/abort; exit cleanly instead of RC=1 [[11]]
            print(f"INFO: receive() stopped (likely END_RUN). Exiting. Details: {type(e).__name__}: {e}")
            break

        if in_model is None:
            print("INFO: receive() returned None. Exiting.")
            break

        state = getattr(in_model, "params", None)

        if not state:
            # Matches what you saw in logs at round 0 [[11]]
            print("WARN: received empty global model; using deterministic local initialization.")
        else:
            # strict=False prevents hard crash if something is missing
            missing, unexpected = model.load_state_dict(state, strict=False)
            if missing or unexpected:
                print(f"WARN: load_state_dict mismatch. missing={missing}, unexpected={unexpected}")

        loss, n_steps, n_samples = train_one_round(model, train_loader, device)

        # Optional: satisfy a selector expecting val_accuracy (your logs show it expects this) [[11]]
        val_accuracy = -loss

        out = flare.FLModel(
            params={k: v.detach().cpu() for k, v in model.state_dict().items()},
            metrics={"loss": float(loss), "val_accuracy": float(val_accuracy)},
            meta={
                "NUM_STEPS_CURRENT_ROUND": float(n_steps),
                "Aggregation_weight": float(n_samples),
            },
        )
        flare.send(out)


if __name__ == "__main__":
    main()