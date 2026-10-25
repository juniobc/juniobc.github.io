"""Chat do dono com a empresa. Enviar exige o token de leitura em cabeçalho (ou a sessão do Google com X-CSRF); nenhuma ação de administrador passa por aqui."""
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..engine import config, owner_chat


class Send(BaseModel):
    to: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")    # slug do funcionário ou "todos"
    body: str = Field(min_length=1, max_length=config.OWNER_MSG_MAX)


def register(app, read_auth, write_auth, conn_fn):
    @app.get("/api/chat", dependencies=[Depends(read_auth)])
    def chat(limit: int = Query(60, ge=1, le=200)):
        c = conn_fn()
        return {"mensagens": owner_chat.thread(c, limit), "turnos_extras_hoje": owner_chat.extra_turns_today(c), "limite_turnos_extras": config.OWNER_TURNS_PER_DAY}

    @app.post("/api/chat", dependencies=[Depends(write_auth)])
    def send(b: Send):
        mid, err = owner_chat.send(conn_fn(), b.to, b.body)
        if err:
            raise HTTPException(429 if "limite" in err else 400, err)
        return {"id": mid}
