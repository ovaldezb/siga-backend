from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List

class VehiculoResumen(BaseModel):
    vehiculo_id: str
    placas: str
    marca: str
    modelo: str
    anio: int = Field(alias="año")

    class Config:
        populate_by_name = True

class ClienteBase(BaseModel):
    nombre: str
    apellido_paterno: str
    apellido_materno: Optional[str] = None
    telefono: str
    email: Optional[EmailStr] = None
    direccion: Optional[str] = None

class ClienteCreate(ClienteBase):
    pass

class VehiculoClienteCreate(BaseModel):
    placas: str
    marca: str
    modelo: str
    anio: int = Field(alias="año")
    vin: Optional[str] = None

    class Config:
        populate_by_name = True
