"""认证目录运维 CLI：创建用户、改密、停用、服务凭据与查看清单。

示例（在仓库根目录、已配置 AUTH_DB_USER/AUTH_DB_PASSWORD 时）：
    python -m backend.scripts.manage_auth list
    python -m backend.scripts.manage_auth create-user --username alice --role analyst
    python -m backend.scripts.manage_auth set-password --username alice
    python -m backend.scripts.manage_auth deactivate --username alice
    python -m backend.scripts.manage_auth create-service --name bi-reader --scopes data:read
    python -m backend.scripts.manage_auth revoke-service --name bi-reader

密码默认交互式输入，避免进入 shell 历史；服务密钥只在创建时输出一次。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import get_settings
from backend.models.database_models import AuthUser, ServiceClient
from backend.utils.auth import (
    KNOWN_SCOPES,
    ROLE_SCOPES,
    effective_scopes,
    hash_password,
    hash_service_key,
    normalize_scopes,
)


def _password(value: str | None) -> str:
    if value:
        return value
    first = getpass.getpass("密码: ")
    second = getpass.getpass("再次输入密码: ")
    if first != second:
        raise SystemExit("两次输入的密码不一致")
    if len(first) < 8:
        raise SystemExit("密码至少 8 位")
    return first


def _parse_scopes(raw: str | None, role: str | None) -> set[str]:
    """用户显式权限在鉴权时与角色默认权限取并集；服务凭据只使用显式权限。"""
    if raw:
        scopes = normalize_scopes(raw)
        unknown = scopes - KNOWN_SCOPES
        if unknown:
            raise SystemExit(f"未知权限: {sorted(unknown)}")
        return scopes
    return set(ROLE_SCOPES.get(role or "viewer", frozenset()))


async def _create_user(session, args) -> None:
    username = args.username.strip()
    if not username:
        raise SystemExit("用户名不能为空")
    scopes = _parse_scopes(args.scopes, args.role)
    session.add(
        AuthUser(
            username=username,
            password_hash=hash_password(_password(args.password)),
            role=args.role,
            scopes=",".join(sorted(scopes)),
            is_active=True,
            token_version=1,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise SystemExit(f"用户已存在: {username}") from exc
    print(f"已创建用户 {username}（角色 {args.role}，权限 {','.join(sorted(scopes))}）")


async def _set_password(session, args) -> None:
    user = await _require_user(session, args.username)
    user.password_hash = hash_password(_password(args.password))
    # 改密后 token_version 自增，所有旧 JWT 立即失效。
    user.token_version = int(user.token_version or 1) + 1
    await session.commit()
    print(f"已更新 {user.username} 的密码，旧会话已失效")


async def _toggle(session, args, active: bool) -> None:
    user = await _require_user(session, args.username)
    user.is_active = active
    if not active:
        user.token_version = int(user.token_version or 1) + 1
    await session.commit()
    print(f"用户 {user.username} 已{'启用' if active else '停用'}")


async def _require_user(session, username: str) -> AuthUser:
    user = (
        await session.execute(select(AuthUser).where(AuthUser.username == username))
    ).scalar_one_or_none()
    if user is None:
        raise SystemExit(f"用户不存在: {username}")
    return user


async def _create_service(session, args) -> None:
    scopes = _parse_scopes(args.scopes, None)
    if not scopes:
        raise SystemExit("服务凭据至少需要一个权限")
    raw_key = secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(UTC).replace(tzinfo=None) + timedelta(days=args.expires_days)
        if args.expires_days
        else None
    )
    session.add(
        ServiceClient(
            name=args.name,
            key_prefix=raw_key[:12],
            key_hash=hash_service_key(raw_key),
            scopes=",".join(sorted(scopes)),
            is_active=True,
            expires_at=expires_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise SystemExit(f"服务凭据名称已存在: {args.name}") from exc
    print(f"已创建服务凭据 {args.name}（权限 {','.join(sorted(scopes))}）")
    print(f"密钥（只显示一次）: {raw_key}")


async def _revoke_service(session, args) -> None:
    client = (
        await session.execute(
            select(ServiceClient).where(ServiceClient.name == args.name)
        )
    ).scalar_one_or_none()
    if client is None:
        raise SystemExit(f"服务凭据不存在: {args.name}")
    client.is_active = False
    await session.commit()
    print(f"服务凭据 {args.name} 已停用")


async def _list(session) -> None:
    users = (await session.execute(select(AuthUser).order_by(AuthUser.username))).scalars()
    print("用户：")
    for user in users:
        scopes = effective_scopes(user.role, user.scopes)
        print(
            f"  {user.username}\trole={user.role}\tactive={bool(user.is_active)}\t"
            f"token_version={user.token_version}\tscopes={','.join(sorted(scopes))}"
        )
    clients = (
        await session.execute(select(ServiceClient).order_by(ServiceClient.name))
    ).scalars()
    print("服务凭据：")
    for client in clients:
        print(
            f"  {client.name}\tprefix={client.key_prefix}\tactive={bool(client.is_active)}\t"
            f"scopes={client.scopes}\texpires={client.expires_at or '-'}"
        )


async def _run(args) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.auth_async_database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            if args.command == "create-user":
                await _create_user(session, args)
            elif args.command == "set-password":
                await _set_password(session, args)
            elif args.command == "deactivate":
                await _toggle(session, args, False)
            elif args.command == "activate":
                await _toggle(session, args, True)
            elif args.command == "create-service":
                await _create_service(session, args)
            elif args.command == "revoke-service":
                await _revoke_service(session, args)
            elif args.command == "list":
                await _list(session)
            else:
                raise SystemExit(f"未知命令: {args.command}")
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="认证目录运维工具")
    sub = parser.add_subparsers(dest="command", required=True)

    create_user = sub.add_parser("create-user", help="创建用户")
    create_user.add_argument("--username", required=True)
    create_user.add_argument(
        "--role", required=True, choices=["admin", "analyst", "viewer", "ingest"]
    )
    create_user.add_argument("--scopes", help="在角色默认权限之外追加的权限（逗号分隔）")
    create_user.add_argument("--password", help="仅用于自动化；省略则交互输入")

    set_password = sub.add_parser("set-password", help="修改密码并吊销旧会话")
    set_password.add_argument("--username", required=True)
    set_password.add_argument("--password", help="仅用于自动化；省略则交互输入")

    for name, text in (("deactivate", "停用用户"), ("activate", "启用用户")):
        toggle = sub.add_parser(name, help=text)
        toggle.add_argument("--username", required=True)

    create_service = sub.add_parser("create-service", help="创建服务凭据")
    create_service.add_argument("--name", required=True)
    create_service.add_argument("--scopes", required=True, help="逗号分隔的权限列表")
    create_service.add_argument("--expires-days", type=int, default=0)

    revoke_service = sub.add_parser("revoke-service", help="停用服务凭据")
    revoke_service.add_argument("--name", required=True)

    sub.add_parser("list", help="查看用户与服务凭据")
    return parser


def main() -> None:
    asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
