from fastapi import APIRouter, UploadFile, File, Depends, Form
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.services.call_service import process_call, process_text
from app.schemas.call import CallLogResponse, ProcessTextRequest

router = APIRouter()


@router.post("/process", response_model=CallLogResponse)
async def process_call_endpoint(
    audio: UploadFile = File(...),
    business_context: str = Form(default=""),
    db: Session = Depends(get_db),
):
    audio_bytes = await audio.read()
    log = await process_call(audio_bytes, db, business_context)
    return log


@router.post("/process-text", response_model=CallLogResponse)
async def process_text_endpoint(
    body: ProcessTextRequest,
    db: Session = Depends(get_db),
):
    log = await process_text(body.text, db, body.business_context)
    return log
