from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List

class VehiculoResumen(BaseModel):
    vehiculo_id: str
    placas: str
    marca: str
    modelo: str
    # Acepta 'anio' (canónico) y 'año' (legacy) en payloads entrantes
    anio: int = Field(validation_alias="anio", serialization_alias="anio")

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
    anio: int = Field(validation_alias="anio", serialization_alias="anio")
    vin: Optional[str] = None

    class Config:
        populate_by_name = True
