"""仓储层（api → service → domain → repository 分层中的最后一环）。

约定（06_Cline开发规则 第 4 条）：
- repository 只负责"受约束的数据访问"（幂等、版本、不可覆盖等硬规则），
  不包含业务判断，也不做网络/解析（属于 collectors / processors）。
- 上层（service / collector）必须通过 repository 修改事实表，不得绕过。
"""

from database.repositories.audit import record_audit
from database.repositories.authors import AuthorAccountRepository, AuthorRepository
from database.repositories.raw_items import supersede_raw_item

__all__ = [
    "AuthorAccountRepository",
    "AuthorRepository",
    "record_audit",
    "supersede_raw_item",
]
