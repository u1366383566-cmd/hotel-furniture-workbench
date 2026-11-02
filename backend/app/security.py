import os
from pathlib import Path
from typing import List, Optional
from fastapi import Header, HTTPException, status

VALID_ROLES = {"operator", "leader", "admin"}

# 身份信任模式：
# - local_sim（默认，本地/第一阶段）：允许通过 X-User-Role/X-User-ID 头模拟角色。
# - trusted_proxy（生产）：后端绝不接受缺失身份；身份头只能由可信认证层
#   （nginx auth_request/JWT 校验）注入，且 nginx 必须先清除外部伪造的同名头。
AUTH_MODE = os.environ.get("WORKBENCH_AUTH_MODE", "local_sim")

class UserContext:
    def __init__(self, user_id: str, role: str):
        self.user_id = user_id
        self.role = role.lower()

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"

    @property
    def is_leader(self) -> bool:
        return self.role == "leader"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

def get_current_user(
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID")
) -> UserContext:
    """
    解析当前请求的用户身份。
    local_sim 模式支持从头部切换角色；trusted_proxy 模式缺失身份直接 401，
    防止绕过 nginx 直连后端伪造身份。
    """
    if AUTH_MODE == "trusted_proxy":
        if not x_user_role or not x_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="身份缺失：生产模式 (trusted_proxy) 必须由可信认证层注入身份头，禁止匿名访问"
            )
        user_id = x_user_id
    else:
        user_id = x_user_id or "simulated_operator"

    role = (x_user_role or "operator").lower().strip()
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"无效的角色身份: {role}，仅允许 operator, leader, admin"
        )
    return UserContext(user_id=user_id or "user", role=role)

def require_roles(allowed_roles: List[str]):
    """要求必须具备指定的角色之一"""
    def role_checker(
        x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
        x_user_id: Optional[str] = Header(None, alias="X-User-ID")
    ) -> UserContext:
        user = get_current_user(x_user_role, x_user_id)
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足: 当前角色 '{user.role}' 无权执行此操作，需要 {allowed_roles}"
            )
        return user
    return role_checker

def safe_file_path(base_dir: Path, filename: str) -> Path:
    """
    严格的路径遍历安全检查，避免任何相对路径穿越。
    """
    # 移除危险的跨目录字符
    clean_name = os.path.basename(filename)
    target_path = (base_dir / clean_name).resolve()
    base_resolved = base_dir.resolve()
    try:
        target_path.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件路径，禁止路径穿越访问"
        )
    return target_path
