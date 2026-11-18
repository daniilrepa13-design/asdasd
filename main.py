from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from pydantic import BaseModel
from datetime import datetime, timedelta
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="Booking API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = "sqlite:///./booking.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- МОДЕЛИ ---
class Resource(Base):
    __tablename__ = "resources"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String); type = Column(String); capacity = Column(Integer)
    buffer_minutes = Column(Integer, default=0); max_parallel = Column(Integer, default=1) 

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, index=True)
    resource_id = Column(Integer, ForeignKey("resources.id"))
    start_time = Column(DateTime); end_time = Column(DateTime)
    user_name = Column(String); is_active = Column(Boolean, default=True)
    cancel_reason = Column(String, nullable=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    password = Column(String) # НОВОЕ ПОЛЕ
    is_admin = Column(Boolean, default=False) # НОВОЕ ПОЛЕ

Base.metadata.create_all(bind=engine)

# --- СХЕМЫ ---
class ResourceCreate(BaseModel):
    name: str; type: str; capacity: int; buffer_minutes: int = 0; max_parallel: int = 1
class BookingCreate(BaseModel):
    resource_id: int; start_time: datetime; end_time: datetime; user_name: str
class BookingUpdate(BaseModel):
    start_time: datetime; end_time: datetime; user_name: str
class CancelRequest(BaseModel):
    reason: str = "Отменено"
class UserLogin(BaseModel):
    name: str; password: str
class UserCreate(BaseModel):
    name: str; password: str

# --- ЭНДПОИНТЫ ---
@app.post("/login")
def login(user: UserLogin):
    db = SessionLocal()
    db_user = db.query(User).filter(User.name == user.name).first()
    if not db_user or db_user.password != user.password:
        db.close()
        raise HTTPException(401, "Неверное имя или пароль")
    db.close()
    return {"name": db_user.name, "is_admin": db_user.is_admin}

@app.get("/users")
def get_users():
    db = SessionLocal(); users = db.query(User).all(); db.close(); return users

@app.post("/users")
def create_user(user: UserCreate):
    db = SessionLocal()
    if db.query(User).filter(User.name == user.name).first():
        db.close(); raise HTTPException(400, "Пользователь уже существует")
    new_user = User(name=user.name, password=user.password, is_admin=False)
    db.add(new_user); db.commit(); db.refresh(new_user); db.close()
    return new_user

@app.post("/resources")
def create_resource(resource: ResourceCreate):
    db = SessionLocal(); new_res = Resource(**resource.dict())
    db.add(new_res); db.commit(); db.refresh(new_res); db.close()
    return new_res

@app.get("/resources")
def get_resources():
    db = SessionLocal(); res = db.query(Resource).all(); db.close(); return res

def check_booking_conflict(db, resource_id, start, end, user_name, exclude_id=None):
    res = db.query(Resource).filter(Resource.id == resource_id).first()
    if not res: raise HTTPException(404, "Ресурс не найден")
    if start < datetime.now(): raise HTTPException(400, "Нельзя бронировать на прошедшее время (FR-9)")
    if start > datetime.now() + timedelta(days=30): raise HTTPException(400, "Нельзя бронировать дальше, чем на 30 дней вперед")
    active_query = db.query(Booking).filter(Booking.user_name == user_name, Booking.is_active == True, Booking.start_time > datetime.now())
    if exclude_id: active_query = active_query.filter(Booking.id != exclude_id)
    if active_query.count() >= 5: raise HTTPException(400, "У вас уже 5 активных броней. Отмените одну.")
    buffer = timedelta(minutes=res.buffer_minutes)
    conflict_query = db.query(Booking).filter(Booking.resource_id == resource_id, Booking.is_active == True, Booking.start_time < end + buffer, Booking.end_time > start - buffer)
    if exclude_id: conflict_query = conflict_query.filter(Booking.id != exclude_id)
    if len(conflict_query.all()) >= res.max_parallel: raise HTTPException(400, f"Конфликт! Лимит броней ({res.max_parallel}) на это время достигнут")
    return True

@app.post("/bookings")
def create_booking(booking: BookingCreate):
    db = SessionLocal()
    if booking.end_time <= booking.start_time: raise HTTPException(400, "Конец должен быть позже начала")
    check_booking_conflict(db, booking.resource_id, booking.start_time, booking.end_time, booking.user_name)
    new_b = Booking(**booking.dict()); db.add(new_b); db.commit(); db.refresh(new_b); db.close()
    return new_b

@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, req: CancelRequest = None):
    db = SessionLocal(); b = db.query(Booking).filter(Booking.id == booking_id).first()
    if not b: raise HTTPException(404, "Бронь не найдена")
    if not b.is_active: raise HTTPException(400, "Бронь уже отменена")
    b.is_active = False; b.cancel_reason = req.reason if req else "Отменено"
    db.commit(); db.close(); return {"message": "OK"}

@app.put("/bookings/{booking_id}")
def update_booking(booking_id: int, booking: BookingUpdate):
    db = SessionLocal(); b = db.query(Booking).filter(Booking.id == booking_id).first()
    if not b or not b.is_active: raise HTTPException(404, "Бронь не найдена")
    check_booking_conflict(db, b.resource_id, booking.start_time, booking.end_time, booking.user_name, exclude_id=booking_id)
    b.start_time = booking.start_time; b.end_time = booking.end_time; b.user_name = booking.user_name
    db.commit(); db.refresh(b); db.close(); return b

@app.get("/bookings")
def get_bookings(user_name: str = None, limit: int = 100):
    db = SessionLocal(); query = db.query(Booking)
    if user_name: query = query.filter(Booking.user_name.ilike(f"%{user_name}%"))
    res = query.order_by(Booking.start_time.desc()).limit(limit).all()
    db.close(); return res

# При первом запуске создаем админа
@app.on_event("startup")
def startup_event():
    db = SessionLocal()
    if not db.query(User).filter(User.name == "admin").first():
        db.add(User(name="admin", password="123123", is_admin=True))
        db.commit()
    db.close()

@app.get("/")
def read_root(): return FileResponse("static/index.html")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)