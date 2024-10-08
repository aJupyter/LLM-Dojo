import torch.nn.functional as F
import torch.nn as nn
import torch


def compute_logprobs(logits, labels, mask=None):
    """
    logits:  shape (batch_size, sequence_len, vocab_size)
    labels:  shape (batch_size, sequence_len)
    """

    # 需要先进行位移操作
    # 去掉标签的第一个
    labels = labels[:, 1:].clone()
    # 去掉模型输出的最后一个
    logits = logits[:, :-1, :]

    logps = F.log_softmax(logits, dim=-1)

    select_logprobs = torch.gather(
        input=logps,
        dim=1,
        index=labels.unsqueeze(1)
    ).squeeze(1)

    if mask is not None:
        mask = mask[:, 1:].clone()
        # 进行掩码padding部分
        select_logprobs = select_logprobs * mask
        # 计算平均
        average_logprobs = select_logprobs.sum(-1) / mask.sum(-1)
        return average_logprobs
    else:
        return select_logprobs.mean(-1)


def compute_batch_loss(batch, policy_model, reference_model, beta):
    """Compute the DPO loss on an input batch"""
    loss_fn = DPOLoss(beta)

    policy_chosen_logps = compute_logprobs(
        logits=policy_model(batch["chosen"]),
        labels=batch["chosen"],
        mask=batch["chosen_mask"]
    )
    policy_rejected_logps = compute_logprobs(
        logits=policy_model(batch["rejected"]),
        labels=batch["rejected"],
        mask=batch["rejected_mask"]
    )
    reference_chosen_logps = compute_logprobs(
        logits=reference_model(batch['chosen']),
        labels=batch['chosen'],
        mask=batch["chosen_mask"]
    )
    reference_rejected_logps = compute_logprobs(
        logits=reference_model(batch['rejected']),
        labels=batch['rejected'],
        mask=batch["rejected_mask"]
    )
    loss, chosen_rewards, rejected_rewards = loss_fn(
        policy_chosen_logps=policy_chosen_logps,
        policy_rejected_logps=policy_rejected_logps,
        reference_chosen_logps=reference_chosen_logps,
        reference_rejected_logps=reference_rejected_logps,
        beta=beta
    )
    return loss, chosen_rewards, rejected_rewards


def compute_dataloader_loss(data_loader, policy_model, reference_model, beta, num_batch=None):
    total_loss, total_chosen_rewards, total_rejected_rewards = 0., 0., 0.
    if len(data_loader) == 0:
        return float("nan")
    elif num_batch is None:
        num_batches = len(data_loader)
    else:
        # Reduce the number of batches to match the total number of batches in the data loader
        # if num_batches exceeds the number of batches in the data loader
        num_batches = min(num_batch, len(data_loader))

    for i, batch in enumerate(data_loader):
        if i < num_batches:
            loss, chosen_rewards, rejected_rewards = compute_batch_loss(
                batch=batch,
                policy_model=policy_model,
                reference_model=reference_model,
                beta=beta
            )
            total_loss += loss.item()
            total_chosen_rewards += chosen_rewards.item()
            total_rejected_rewards += rejected_rewards.item()

        else:
            break
    total_loss /= num_batches
    total_chosen_rewards /= num_batches
    total_rejected_rewards /= num_batches
    return total_loss, total_chosen_rewards, total_rejected_rewards


class DPOLoss(nn.Module):
    """
    DPO Loss
    """

    def __init__(self, beta: float = 0.1) -> None:
        super().__init__()
        self.beta = beta

    def forward(
            self,
            policy_chosen_logps: torch.Tensor,
            policy_rejected_logps: torch.Tensor,
            reference_chosen_logps: torch.Tensor,
            reference_rejected_logps: torch.Tensor,
    ):
        """
        policy_chosen_logps: 模型输出的对数概率。Shape: (batch_size,)
        policy_rejected_logps:   Shape: (batch_size,)
        reference_chosen_logps: Shape: (batch_size,)
        reference_rejected_logps: Shape: (batch_size,)

        """
        policy_logps = policy_chosen_logps - policy_rejected_logps
        reference_logps = reference_chosen_logps - reference_rejected_logps
        logits = policy_logps - reference_logps

        loss = -F.logsigmoid(self.beta * logits)

        # 下面两个用于追踪训练的进度
        chosen_rewards = (policy_chosen_logps - reference_chosen_logps).detach()
        rejected_rewards = (policy_rejected_logps - reference_rejected_logps).detach()

        # 对每个batch进行平均
        return loss.mean(), chosen_rewards.mean(), rejected_rewards.mean()


def evaluate_dpo_loss_loader(policy_model, reference_model, train_loader, val_loader, beta, eval_iter):
    """Compute the DPO loss for the training and validation dataset"""

    policy_model.eval()
    with torch.no_grad():
        train_loss, train_chosen_rewards, train_rejected_rewards = compute_dataloader_loss(
            data_loader=train_loader,
            policy_model=policy_model,
            reference_model=reference_model,
            beta=beta,
            num_batches=eval_iter
        )

        val_loss, val_chosen_rewards, val_rejected_rewards = compute_dataloader_loss(
            data_loader=val_loader,
            policy_model=policy_model,
            reference_model=reference_model,
            beta=beta,
            num_batches=eval_iter
        )

    res = {
        "train_loss": train_loss,
        "train_chosen_reward": train_chosen_rewards,
        "train_rejected_reward": train_rejected_rewards,
        "val_loss": val_loss,
        "val_chosen_reward": val_chosen_rewards,
        "val_rejected_reward": val_rejected_rewards
    }

    policy_model.train()
    return res


# 开始训练模型
def train_model(
        policy_model, reference_model, train_loader, val_loader,
        optimizer, num_epochs, beta,
        eval_freq, eval_iter, start_context, tokenizer
):
    tracking = {
        "train_losses": [],
        "train_chosen_rewards": [],
        "train_rejected_rewards": [],
        "val_losses": [],
        "val_chosen_rewards": [],
        "val_rejected_rewards": [],
        "tokens_seen": []
    }

    tokens_seen, global_step = 0, -1

    # 训练
    for epoch in range(num_epochs):
        # policy 模型需要训练
        policy_model.train()

        for idx, batch in enumerate(train_loader):
            optimizer.zero_grad()

            loss, chosen_rewards, rejected_rewards = compute_batch_loss(
                batch=batch,
                policy_model=policy_model,
                reference_model=reference_model,
                beta=beta
            )

            loss.backward()
            optimizer.step()

            global_step += 1
            tokens_seen += batch["chosen"].numel()

            # 验证
            if global_step % eval_freq == 0:
                res = evaluate_dpo_loss_loader(
                    policy_model=policy_model,
                    reference_model=reference_model,
                    train_loader=train_loader,
                    val_loader=val_loader,
                    beta=beta,
                    eval_iter=eval_iter
                )
                tracking["train_losses"].append(res["train_loss"])
                tracking["train_chosen_rewards"].append(res["train_chosen_reward"])
                tracking["train_rejected_rewards"].append(res["train_rejected_reward"])
                tracking["val_losses"].append(res["val_loss"])
                tracking["val_chosen_rewards"].append(res["val_chosen_reward"])
                tracking["val_rejected_rewards"].append(res["val_rejected_reward"])
                tracking["tokens_seen"].append(tokens_seen)
                train_reward_margin = res["train_chosen_reward"] - res["train_rejected_reward"]
                val_reward_margin = res["val_chosen_reward"] - res["val_rejected_reward"]

                print(
                    f"Ep {epoch + 1} (Step {global_step:06d}): "
                    f"Train loss {res['train_loss']:.3f}, Val loss {res['val_loss']:.3f}, "
                    f"Train reward margins {train_reward_margin:.3f}, "
                    f"Val reward margins {val_reward_margin:.3f}"
                )

    return tracking


def main():
    pass
