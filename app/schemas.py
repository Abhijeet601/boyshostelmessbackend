from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import re


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(Model):
    portal: Literal["super-admin", "admin", "student"] | None = None
    login_id: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(Model):
    token: str
    role: str
    user: dict


class StudentCreateRequest(Model):
    admission_number: str = Field(min_length=2, max_length=100)
    registration_number: str = Field(min_length=2, max_length=100)
    name: str = Field(min_length=2, max_length=150)
    email: str | None = Field(default=None, max_length=150)
    mobile: str | None = Field(default=None, max_length=30)
    hostel: str = Field(min_length=1, max_length=120)
    room_number: str = Field(min_length=1, max_length=40)
    course: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    academic_year: str | None = Field(default=None, max_length=40)
    semester: str | None = Field(default=None, max_length=40)
    academic_session: str | None = Field(default=None, max_length=80)
    photo_url: str | None = Field(default=None, max_length=2000)
    active: bool = True
    qr_enabled: bool = True
    login_id: str | None = Field(default=None, min_length=2, max_length=100)
    password: str | None = Field(default=None, min_length=10, max_length=256)

    @field_validator("photo_url")
    @classmethod
    def photo_scheme(cls, value):
        if value and not value.startswith(("https://", "http://", "/")):
            raise ValueError("Use an HTTP(S) image URL")
        return value


class UserCreateRequest(Model):
    login_id: str = Field(min_length=2, max_length=100)
    password: str | None = Field(default=None, min_length=10, max_length=256)
    role: Literal["admin", "super-admin", "student"] = "admin"
    name: str = Field(min_length=2, max_length=150)
    email: str | None = Field(default=None, max_length=150)
    mobile: str | None = Field(default=None, max_length=30)
    photo_url: str | None = Field(default=None, max_length=2000)
    hostel_scope: str | None = Field(default=None, max_length=120)
    student_id: int | None = None
    active: bool = True
    scanner_enabled: bool = True


class HostelRequest(Model):
    name: str = Field(min_length=1, max_length=120)
    contact: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1000)
    active: bool = True


class QRConsumeRequest(Model):
    token: str = Field(min_length=1, max_length=256)
    meal: Literal["Breakfast", "Lunch", "Snacks", "Dinner"] | None = None
    device_mode: Literal["camera", "hardware"] = "hardware"


class SessionRequest(Model):
    meal: Literal["Breakfast", "Lunch", "Snacks", "Dinner"]
    session_date: date
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    hostel: str = Field(default="*", max_length=120)
    active: bool = True


class ProfileChangeCreate(Model):
    changes: dict[str, str | None]


class ProfileChangeDecision(Model):
    decision: Literal["Approved", "Rejected"]
    admin_note: str | None = Field(default=None, max_length=1000)


class MealMenuUpsert(Model):
    menu_date: date
    breakfast: str = Field(default="", max_length=2000)
    lunch: str = Field(default="", max_length=2000)
    snacks: str = Field(default="", max_length=2000)
    dinner: str = Field(default="", max_length=2000)


class PasswordChange(Model):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class ProfileUpdate(Model):
    name: str = Field(min_length=2, max_length=150)
    email: str | None = Field(default=None, max_length=150)
    mobile: str | None = Field(default=None, max_length=30)
    photo_url: str | None = Field(default=None, max_length=2000)


class Announcement(Model):
    title: str = Field(min_length=2, max_length=150)
    body: str = Field(min_length=2, max_length=2000)


class RegistrationRequest(Model):
    @field_validator("*", mode="before")
    @classmethod
    def trim_text(cls, value, info):
        return value.strip() if isinstance(value, str) and info.field_name not in {"password", "confirm_password", "photo_data"} else value
    name: str = Field(min_length=2, max_length=150)
    email: str = Field(max_length=150)
    mobile: str = Field(pattern=r"^\+?[0-9][0-9 -]{7,18}[0-9]$")
    admission_number: str = Field(min_length=2, max_length=100)
    registration_number: str = Field(min_length=2, max_length=100)
    hostel: str = Field(min_length=1, max_length=120)
    room_number: str = Field(min_length=1, max_length=40)
    course: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=120)
    academic_year: str = Field(min_length=1, max_length=40)
    semester: str = Field(min_length=1, max_length=40)
    academic_session: str = Field(min_length=1, max_length=80)
    login_id: str = Field(min_length=2, max_length=100, pattern=r"^[A-Za-z0-9@._+\-]+$")
    password: str = Field(min_length=10, max_length=256)
    confirm_password: str = Field(max_length=256)
    photo_data: str | None = Field(default=None, max_length=4200000)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value.lower()

    @model_validator(mode="after")
    def passwords(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        if not (re.search(r"[A-Za-z]", self.password) and re.search(r"\d", self.password) and re.search(r"[^A-Za-z0-9]", self.password)):
            raise ValueError("Use at least 10 characters with a letter, number and symbol")
        return self


class RegistrationDecision(Model):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(default="", max_length=1000)


class AccountState(Model):
    active: bool
    reason: str = Field(min_length=3, max_length=1000)
