"""跨内部 Adapter 保留应用层稳定失败分类的标记。"""


class PreservedApplicationFailure(RuntimeError):
    """已由应用层稳定分类、穿越内部 Adapter 时不得改写的失败。"""


__all__ = ["PreservedApplicationFailure"]
