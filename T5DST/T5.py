# Copyright (c) Facebook, Inc. and its affiliates

import json
import os

import pytorch_lightning as pl
import torch
from config import get_args
from data_loader import prepare_data
from evaluate import evaluate_metrics
from pytorch_lightning import Trainer, seed_everything
from tqdm import tqdm
from transformers import AdamW, AutoModelForSeq2SeqLM, AutoTokenizer


class DST_Seq2Seq(pl.LightningModule):
    def __init__(self, args, tokenizer, model):
        super().__init__()
        self.tokenizer = tokenizer
        self.model = model
        self.lr = args["lr"]

    def forward(self, batch, *args, **kwargs):
        return self.model(
            input_ids=batch["encoder_input"],
            attention_mask=batch["attention_mask"],
            lm_labels=batch["decoder_output"],
        )

    def training_step(self, batch, batch_idx):
        (loss), *_ = self.forward(batch)
        return {"loss": loss, "log": {"train_loss": loss}}

    def validation_step(self, batch, batch_idx):
        (loss), *_ = self.forward(batch)
        return {"val_loss": loss, "log": {"val_loss": loss}}

    def validation_epoch_end(self, outputs):
        val_loss_mean = sum([o["val_loss"] for o in outputs]) / len(outputs)
        results = {
            "progress_bar": {"val_loss": val_loss_mean.item()},
            "log": {"val_loss": val_loss_mean.item()},
            "val_loss": val_loss_mean.item(),
        }
        return results

    def configure_optimizers(self):
        return AdamW(self.parameters(), lr=self.lr, correct_bias=True)


def train(args):
    args = vars(args)
    args["model_name"] = (
        args["model_checkpoint"]
        + args["model_name"]
        + "_except_domain_"
        + args["except_domain"]
        + "_slotlang_"
        + str(args["slot_lang"])
        + "_lr_"
        + str(args["lr"])
        + "_epoch_"
        + str(args["n_epochs"])
        + "_seed_"
        + str(args["seed"])
    )
    seed_everything(args["seed"])

    model = AutoModelForSeq2SeqLM.from_pretrained(args["model_checkpoint"])
    tokenizer = AutoTokenizer.from_pretrained(
        args["model_checkpoint"],
        bos_token="[bos]",
        eos_token="[eos]",
        sep_token="[sep]",
    )
    model.resize_token_embeddings(new_num_tokens=len(tokenizer))

    task = DST_Seq2Seq(args, tokenizer, model)

    (
        train_loader,
        val_loader,
        test_loader,
        ALL_SLOTS,
        fewshot_loader_dev,
        fewshot_loader_test,
    ) = prepare_data(args, task.tokenizer)

    save_path = os.path.join(args["saving_dir"], args["model_name"])
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    trainer = Trainer(
        default_root_dir=save_path,
        accumulate_grad_batches=args["gradient_accumulation_steps"],
        gradient_clip_val=args["max_norm"],
        max_epochs=args["n_epochs"],
        callbacks=[
            pl.callbacks.EarlyStopping(
                monitor="val_loss",
                min_delta=0.00,
                patience=5,
                verbose=False,
                mode="min",
            )
        ],
        gpus=args["GPU"],
        # deterministic=True,
        num_nodes=1,
        precision=16,
        accelerator="ddp",
    )

    trainer.fit(task, train_loader, val_loader)

    task.model.save_pretrained(save_path)
    task.tokenizer.save_pretrained(save_path)

    print("test start...")
    # evaluate model
    _ = evaluate_model(
        args, task.tokenizer, task.model, test_loader, save_path, ALL_SLOTS
    )


def evaluate_model(
    args, tokenizer, model, test_loader, save_path, ALL_SLOTS, prefix="zeroshot"
):
    save_path = os.path.join(save_path, "results")
    if not os.path.exists(save_path):
        os.makedirs(save_path)
    predictions = {}
    # to gpu
    # gpu = args["GPU"][0]
    device = torch.device("cuda:0")
    model.to(device)
    model.eval()

    slot_logger = {slot_name: [0, 0, 0] for slot_name in ALL_SLOTS}

    for batch in tqdm(test_loader):
        dst_outputs = model.generate(
            input_ids=batch["encoder_input"].to(device),
            attention_mask=batch["attention_mask"].to(device),
            eos_token_id=tokenizer.eos_token_id,
            max_length=200,
        )

        value_batch = tokenizer.batch_decode(dst_outputs, skip_special_tokens=True)

        for idx, value in enumerate(value_batch):
            dial_id = batch["ID"][idx]
            if dial_id not in predictions:
                predictions[dial_id] = {}
                predictions[dial_id]["domain"] = batch["domains"][idx][0]
                predictions[dial_id]["turns"] = {}
            if batch["turn_id"][idx] not in predictions[dial_id]["turns"]:
                predictions[dial_id]["turns"][batch["turn_id"][idx]] = {
                    "turn_belief": batch["turn_belief"][idx],
                    "pred_belief": [],
                }

            if value != "none":
                predictions[dial_id]["turns"][batch["turn_id"][idx]][
                    "pred_belief"
                ].append(str(batch["slot_text"][idx]) + "-" + str(value))

            # analyze slot acc:
            if str(value) == str(batch["value_text"][idx]):
                slot_logger[str(batch["slot_text"][idx])][1] += 1  # hit
            slot_logger[str(batch["slot_text"][idx])][0] += 1  # total

    for slot_log in slot_logger.values():
        slot_log[2] = slot_log[1] / slot_log[0]

    with open(os.path.join(save_path, f"{prefix}_slot_acc.json"), "w") as f:
        json.dump(slot_logger, f, indent=4)

    with open(os.path.join(save_path, f"{prefix}_prediction.json"), "w") as f:
        json.dump(predictions, f, indent=4)

    joint_acc_score, F1_score, turn_acc_score = evaluate_metrics(predictions, ALL_SLOTS)

    evaluation_metrics = {
        "Joint Acc": joint_acc_score,
        "Turn Acc": turn_acc_score,
        "Joint F1": F1_score,
    }
    print(f"{prefix} result:", evaluation_metrics)

    with open(os.path.join(save_path, f"{prefix}_result.json"), "w") as f:
        json.dump(evaluation_metrics, f, indent=4)

    return predictions


def fine_tune(args):
    args = vars(args)
    seed_everything(args["seed"])
    domains = ["hotel", "train", "restaurant", "attraction", "taxi"]
    for domain in domains:
        if domain in args["model_checkpoint"]:
            args["only_domain"] = domain
    assert args["only_domain"] != "none"
    print(args)

    model = AutoModelForSeq2SeqLM.from_pretrained(args["model_checkpoint"])
    tokenizer = AutoTokenizer.from_pretrained(
        args["model_checkpoint"],
        bos_token="[bos]",
        eos_token="[eos]",
        sep_token="[sep]",
    )

    task = DST_Seq2Seq(args, tokenizer, model)
    (
        train_loader,
        val_loader,
        test_loader,
        ALL_SLOTS,
        fewshot_loader_dev,
        fewshot_loader_test,
    ) = prepare_data(args, tokenizer)

    trainer = Trainer(
        default_root_dir=args["model_checkpoint"],
        accumulate_grad_batches=args["gradient_accumulation_steps"],
        gradient_clip_val=args["max_norm"],
        max_epochs=20,
        callbacks=[
            pl.callbacks.EarlyStopping(
                monitor="val_loss",
                min_delta=0.00,
                patience=8,
                verbose=False,
                mode="min",
            )
        ],
        gpus=args["GPU"],
        deterministic=True,
        num_nodes=1,
        accelerator="dp",
    )

    trainer.fit(task, train_loader, val_loader)

    print("test start...")
    ratio = "ratio_" + str(args["fewshot"]) + "_seed_" + str(args["seed"])
    _ = evaluate_model(
        args,
        task.tokenizer,
        task.model,
        test_loader,
        args["model_checkpoint"],
        ALL_SLOTS,
        prefix=ratio,
    )


if __name__ == "__main__":
    args = get_args()
    if args.mode == "train":
        train(args)
    if args.mode == "finetune":
        fine_tune(args)
