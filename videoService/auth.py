import os
import jwt
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, Request

from config import db, users

def authorize(request: Request, owner: str, project: str, edit: bool = False, allow_anonymous: bool = False, deleting: bool = False):
    try:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise ValueError()
        payload = jwt.decode(
            header[7:],
            os.environ["JWT_SECRET_KEY"],
            algorithms=["HS256"],
            options={"require": ["exp", "id"]}
        )
        user = users.find_one({"_id": ObjectId(payload["id"])})
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, InvalidId):
        raise HTTPException(401, "Sessão inválida ou expirada.")

    if not user or user.get("type") not in (("anonymous", "free", "premium") if allow_anonymous else ("free", "premium")):
        raise HTTPException(403, "O vídeo requer um perfil registado ou premium.")

    try:
        doc = db.projects.find_one({"_id": ObjectId(project), "user_id": ObjectId(owner)})
    except Exception:
        raise HTTPException(404, "Projeto não encontrado.")

    if not doc:
        raise HTTPException(404, "Projeto não encontrado.")

    if doc.get("video_deleting") and not deleting:
        raise HTTPException(409, "O projeto está a ser eliminado. Aguarde e atualize a página.")

    can_edit = str(user["_id"]) == owner
    if not can_edit:
        link = next(
            (s for s in doc.get("sharedLinks", []) if s["id"] == request.query_params.get("share") and not s.get("revoked")),
            None
        )
        if not link:
            raise HTTPException(403, "Sem acesso ao projeto.")
        can_edit = link["permission"] == "edit"

    if edit and not can_edit:
        raise HTTPException(403, "O link permite apenas leitura.")

    return user, can_edit