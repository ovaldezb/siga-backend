from pydantic import BaseModel, Field, validator
from typing import Optional, Literal
from decimal import Decimal

class ItemBase(BaseModel):
    tipo: Literal["PRODUCTO", "SERVICIO"]
    codigo: str
    nombre: str
    precio_venta: float
    categoria: Optional[str] = None

class ItemCreate(ItemBase):
    precio_compra: Optional[float] = None
    maneja_inventario: bool = False
    stock: Optional[int] = 0
    clave_sat: Optional[str] = None
    unidad_sat: Optional[str] = None

    @validator('maneja_inventario', always=True)
    def validate_tipo_inventario(cls, v, values):
        if values.get('tipo') == 'SERVICIO':
            return False
        return v

    @validator('stock', always=True)
    def validate_stock_tipo(cls, v, values):
        if values.get('tipo') == 'SERVICIO':
            return 0
        return v

class StockAdjustment(BaseModel):
    cantidad: int # +n o -n
    motivo: Optional[str] = "Ajuste manual"
