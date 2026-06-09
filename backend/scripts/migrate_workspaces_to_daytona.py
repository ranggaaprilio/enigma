import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import async_session_maker
from app.models import RepositoryCredential, Workspace
from app.services.daytona_runtime import DaytonaRuntime
from app.services.workspaces import WorkspaceSecretCipher


async def migrate() -> None:
    settings = get_settings()
    runtime = DaytonaRuntime(settings)
    cipher = WorkspaceSecretCipher(settings.workspace_env_encryption_key)
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Workspace).where(Workspace.daytona_sandbox_id.is_(None))
            )
            for workspace in result.scalars().all():
                print(f"migrating {workspace.id} ({workspace.name})")
                workspace.status = "creating"
                workspace.runtime = "daytona"
                workspace.workspace_path = settings.daytona_workspace_path
                session.add(workspace)
                await session.commit()

                sandbox = await runtime.provision(
                    workspace.id,
                    workspace.owner_id,
                    workspace.snapshot,
                )
                credential_result = await session.execute(
                    select(RepositoryCredential).where(
                        RepositoryCredential.workspace_id == workspace.id
                    )
                )
                credential = credential_result.scalar_one_or_none()
                token = cipher.decrypt(credential.encrypted_token) if credential else None
                try:
                    await sandbox.fs.get_file_info(f"{workspace.workspace_path}/.git")
                except Exception:
                    await sandbox.git.clone(
                        workspace.repo_url,
                        workspace.workspace_path,
                        branch=workspace.default_branch,
                        username="x-access-token" if token else None,
                        password=token,
                    )

                workspace.daytona_sandbox_id = sandbox.id
                workspace.status = "ready"
                session.add(workspace)
                await session.commit()
                print(f"migrated {workspace.id} -> {sandbox.id}")
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(migrate())
