"""
成本追踪器（Cost Tracker）
API 调用成本估算 + 预算门控
"""
from dataclasses import dataclass, field
from datetime import datetime
import json


# ─── 模型价格（元/百万token）───
MODEL_RATES = {
    "deepseek-chat": {"input": 1.0, "output": 2.0},
    "deepseek-v4-flash": {"input": 1.0, "output": 2.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    "qwen3.6-35b": {"input": 0, "output": 0},       # 本地部署，免费
    "default": {"input": 2.0, "output": 4.0},
}


def estimate_tokens_chinese(text: str) -> int:
    """估算中文字符的 token 数（约 0.6 token/字符）"""
    import re
    chinese = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', text))
    other = len(text) - chinese
    return int(chinese * 0.6 + other * 0.3)


def estimate_cost(model: str, input_text: str, output_tokens: int) -> float:
    """预估一次 API 调用成本"""
    rates = MODEL_RATES.get(model, MODEL_RATES["default"])
    input_tokens = estimate_tokens_chinese(input_text)
    cost = (input_tokens / 1_000_000) * rates["input"] + \
           (output_tokens / 1_000_000) * rates["output"]
    return round(cost, 4)


@dataclass
class CostRecord:
    """单次调用记录"""
    timestamp: str = ""
    operation: str = ""          # 操作类型：outline/chapter/summary/review/de_ai/...
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclass
class CostTracker:
    """成本追踪器"""
    model: str = "deepseek-chat"
    budget: float = 50.0         # 单书预算上限（元）
    spent: float = 0.0
    records: list[CostRecord] = field(default_factory=list)
    book_id: str = ""

    def estimate(self, input_text: str, output_tokens: int = 4096) -> float:
        """预估成本"""
        return estimate_cost(self.model, input_text, output_tokens)

    def check_budget(self, input_text: str, output_tokens: int = 4096) -> bool:
        """预算门控：是否允许本次调用"""
        estimated = self.estimate(input_text, output_tokens)
        return self.spent + estimated <= self.budget

    def remaining(self) -> float:
        return max(0, self.budget - self.spent)

    def record(self, operation: str, input_text: str,
               output_text: str = "", output_tokens: int = 0):
        """记录一次实际调用"""
        input_tokens = estimate_tokens_chinese(input_text)
        if not output_tokens and output_text:
            output_tokens = estimate_tokens_chinese(output_text)
        # estimate() 内部用 estimate_cost(self.model, ...) 重新估算输入 token，
        # 与上面 input_tokens 计算一致，这里直接用即可
        cost = self.estimate(input_text, output_tokens)

        self.spent += cost
        self.records.append(CostRecord(
            timestamp=datetime.now().isoformat(),
            operation=operation,
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
        ))

    def summary(self) -> dict:
        """成本摘要"""
        by_op = {}
        for r in self.records:
            by_op.setdefault(r.operation, 0)
            by_op[r.operation] += r.cost
        return {
            "budget": self.budget,
            "spent": round(self.spent, 4),
            "remaining": round(self.remaining(), 4),
            "total_calls": len(self.records),
            "by_operation": {k: round(v, 4) for k, v in by_op.items()},
        }

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "model": self.model, "budget": self.budget,
                "spent": self.spent, "book_id": self.book_id,
                "records": [r.__dict__ for r in self.records],
            }, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "CostTracker":
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            t = CostTracker(model=d.get("model", "deepseek-chat"),
                            budget=d.get("budget", 50),
                            book_id=d.get("book_id", ""))
            t.spent = d.get("spent", 0)
            t.records = [CostRecord(**r) for r in d.get("records", [])]
            return t
        except (FileNotFoundError, json.JSONDecodeError):
            return CostTracker()
