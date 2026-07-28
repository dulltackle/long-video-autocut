"""跨业务阶段共享的成功结果判别。"""

from enum import Enum


class ResultKind(str, Enum):
    """成功直播拆条运行形成的两类合法业务结果。"""

    CLIPS = "clips"
    EMPTY = "empty"
