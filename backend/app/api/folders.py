import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_user
from app.models.db import DocumentFolder, Folder, get_session
from app.models.schemas import FolderCreate, FolderOut, FolderRename

router = APIRouter(prefix="/api/folders", tags=["folders"])


async def _folder_out(folder: Folder, session: AsyncSession) -> FolderOut:
    count = (
        await session.execute(
            select(func.count()).select_from(DocumentFolder).where(DocumentFolder.folder_id == folder.folder_id)
        )
    ).scalar_one()
    return FolderOut(folder_id=folder.folder_id, name=folder.name, created_at=folder.created_at, document_count=count)


@router.post("", response_model=FolderOut)
async def create_folder(
    body: FolderCreate,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> FolderOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Folder name cannot be empty")
    folder = Folder(name=name)
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    return await _folder_out(folder, session)


@router.get("", response_model=list[FolderOut])
async def list_folders(
    _admin: str = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[FolderOut]:
    folders = (await session.execute(select(Folder).order_by(Folder.name))).scalars().all()
    # One count query per folder is fine at this scale (a handful of
    # folders, not thousands) -- keeps _folder_out reusable for the
    # single-folder create/rename responses too.
    return [await _folder_out(f, session) for f in folders]


@router.patch("/{folder_id}", response_model=FolderOut)
async def rename_folder(
    folder_id: uuid.UUID,
    body: FolderRename,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> FolderOut:
    folder = await session.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Folder name cannot be empty")
    folder.name = name
    await session.commit()
    return await _folder_out(folder, session)


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: uuid.UUID,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    folder = await session.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    # document_folders rows cascade-delete at the DB level (ondelete="CASCADE"
    # on the FK, see models/db.py) -- the documents themselves are untouched,
    # they just lose this membership and become unfiled if it was their only one.
    await session.delete(folder)
    await session.commit()
    return {"folder_id": str(folder_id), "deleted": True}
