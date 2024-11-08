from dataclasses import dataclass, field
from transformers import TrainingArguments


@dataclass
class BaseConfig(TrainingArguments):
    """
    训练参数
    """
    output_dir: str = field(default='./output', metadata={"help": "模型训练完成后的保存路径"})
    num_train_epochs: int = 1,

    per_device_train_batch_size: int = 2
    gradient_checkpointing: bool = True
    gradient_accumulation_steps: int = 16,

    learning_rate: float = 2e-4
    logging_steps: int = 10
    save_steps: int = 500
    save_strategy: str = "steps"
    save_total_limit: int = 2
    lr_scheduler_type: str = "cosine",
    warmup_steps: int = 10
    optim: str = 'adamw_torch'
    report_to: str = 'tensorboard'
    remove_unused_columns: bool = False
    bf16: bool = False
    fp16: bool = False