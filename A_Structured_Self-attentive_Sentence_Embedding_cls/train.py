import argparse
import pickle
import torch
import torch.nn as nn
import torch.optim as optim
from pathlib import Path
from tqdm import tqdm
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.tensorboard import SummaryWriter
from model.net import SAN
from model.split import split_morphs
from model.data import Corpus, batchify
from model.utils import Tokenizer
from model.metric import evaluate, acc
from utils import Config, CheckpointManager, SummaryManager


def get_tokenizer(dataset_config, split_fn=split_morphs):
    with open(dataset_config.vocab, mode="rb") as io:
        vocab = pickle.load(io)
    tokenizer = Tokenizer(vocab=vocab, split_fn=split_fn)
    return tokenizer


def get_data_loaders(dataset_config, tokenizer, batch_size, collate_fn):
    tr_ds = Corpus(dataset_config.train, tokenizer.split_and_transform)
    tr_dl = DataLoader(tr_ds, batch_size=batch_size, shuffle=True, num_workers=4, drop_last=True,
                       collate_fn=collate_fn)
    val_ds = Corpus(dataset_config.validation, tokenizer.split_and_transform)
    val_dl = DataLoader(val_ds, batch_size=batch_size, num_workers=4, collate_fn=collate_fn)
    return tr_dl, val_dl


def regularize(attn_mat, r, device):
    sim_mat = torch.bmm(attn_mat, attn_mat.permute(0, 2, 1))
    identity = torch.eye(r).to(device)
    p = torch.norm(sim_mat - identity, p=2, dim=(1, 2)).mean()
    return p


def main(args):
    dataset_config = Config(args.dataset_config)
    model_config = Config(args.model_config)

    exp_dir = Path("experiments") / model_config.type
    exp_dir = exp_dir.joinpath(
        f"epochs_{args.epochs}_batch_size_{args.batch_size}_learning_rate_{args.learning_rate}"
    )

    if not exp_dir.exists():
        exp_dir.mkdir(parents=True)

    if args.fix_seed:
        torch.manual_seed(777)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    tokenizer = get_tokenizer(dataset_config)
    tr_dl, val_dl = get_data_loaders(dataset_config, tokenizer, args.batch_size, collate_fn=batchify)
    model = SAN(num_classes=model_config.num_classes, lstm_hidden_dim=model_config.lstm_hidden_dim,
                da=model_config.da, r=model_config.r, hidden_dim=model_config.hidden_dim, vocab=tokenizer.vocab)

    loss_fn = nn.CrossEntropyLoss()
    opt = optim.Adam(params=model.parameters(), lr=args.learning_rate)
    scheduler = ReduceLROnPlateau(opt, patience=5)
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model.to(device)

    writer = SummaryWriter(f"{exp_dir}/runs")
    checkpoint_manager = CheckpointManager(exp_dir)
    summary_manager = SummaryManager(exp_dir)
    best_val_loss = 1e+10

    for epoch in tqdm(range(args.epochs), desc="epochs"):

        tr_loss = 0
        tr_acc = 0

        model.train()
        for step, mb in tqdm(enumerate(tr_dl), desc="steps", total=len(tr_dl)):
            x_mb, y_mb = map(lambda elm: elm.to(device), mb)

            opt.zero_grad()
            score, attn_mat = model(x_mb)
            reg = regularize(attn_mat, model_config.r, device)
            mb_loss = loss_fn(score, y_mb)
            mb_loss.add_(reg)
            mb_loss.backward()
            opt.step()

            with torch.no_grad():
                mb_acc = acc(score, y_mb)

            tr_loss += mb_loss.item()
            tr_acc += mb_acc.item()

            if (epoch * len(tr_dl) + step) % args.summary_step == 0:
                val_loss = evaluate(model, val_dl, {"loss": loss_fn}, device)["loss"]
                writer.add_scalars("loss", {"train": tr_loss / (step + 1),
                                            "val": val_loss}, epoch * len(tr_dl) + step)
                model.train()
        else:
            tr_loss /= (step + 1)
            tr_acc /= (step + 1)

            tr_summary = {"loss": tr_loss, "acc": tr_acc}
            val_summary = evaluate(model, val_dl, {"loss": loss_fn, "acc": acc}, device)
            scheduler.step(val_summary['loss'])
            tqdm.write(f"epoch: {epoch+1}\n"
                       f"tr_loss: {tr_summary['loss']:.3f}, val_loss: {val_summary['loss']:.3f}\n"
                       f"tr_acc: {tr_summary['acc']:.2%}, val_acc: {val_summary['acc']:.2%}")

            val_loss = val_summary["loss"]
            is_best = val_loss < best_val_loss

            if is_best:
                state = {"epoch": epoch + 1,
                         "model_state_dict": model.state_dict(),
                         "opt_state_dict": opt.state_dict()}
                summary = {"train": tr_summary, "validation": val_summary}

                summary_manager.update(summary)
                summary_manager.save("summary.json")
                checkpoint_manager.save_checkpoint(state, "best.tar")

                best_val_loss = val_loss


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument_group("config")
    parser.add_argument(
        "--dataset_config",
        default="conf/dataset/nsmc.json",
        help="directory containing nsmc.json",
    )
    parser.add_argument(
        "--model_config",
        default="conf/model/san.json",
        help="directory containing san.json",
    )
    parser.add_argument("--epochs", default=5, help="number of epochs of training")
    parser.add_argument("--batch_size", default=256, help="batch size of training")
    parser.add_argument(
        "--learning_rate", default=1e-3, help="learning rate of training"
    )
    parser.add_argument(
        "--summary_step", default=500, help="logging performance at each step"
    )
    parser.add_argument("--fix_seed", action="store_true", default=False)
    args = parser.parse_args()
    main(args)
