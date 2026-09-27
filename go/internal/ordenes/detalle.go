package ordenes

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// Campos con importe en cada item de un punto y en cada refacción surtida
// (_ITEM_CAMPOS_DINERO).
var camposDineroItem = []string{"precioVenta", "precio_venta", "precioCompra", "precio_compra",
	"subtotal", "costo", "costo_proveedor", "descuento", "importe"}

// quitarImportes replica _sin_importes: la orden sin un solo número de dinero,
// para el mecánico. Muta el mapa.
func quitarImportes(o map[string]any) {
	for _, c := range camposDinero {
		delete(o, c)
	}
	for _, p := range lista(o["puntosArreglar"]) {
		punto, _ := p.(map[string]any)
		for _, it := range lista(punto["items"]) {
			borrar(it, camposDineroItem)
		}
	}
	// `inventario` es la lista de refacciones surtidas; conserva costos por línea.
	for _, e := range lista(o["inventario"]) {
		borrar(e, camposDineroItem)
	}
}

func borrar(v any, campos []string) {
	if m, ok := v.(map[string]any); ok {
		for _, c := range campos {
			delete(m, c)
		}
	}
}

// Get atiende GET /ordenes/{id} (port de get_orden_handler): la orden con los
// datos actuales de su vehículo y, si está cotizada, si ya se envió el enlace
// al cliente.
//
// PENDIENTE (igual que en Python): no valida el scope de sucursal del usuario.
// Ver la nota en list_ordenes_handler.
func Get(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	oid, err := platform.ParseObjectID(req.PathParameters["id"], "id")
	if err != nil {
		return platform.Response{}, err
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	var doc bson.M
	err = db.Collection("ordenes_servicio").FindOne(ctx, bson.D{{Key: "_id", Value: oid}}).Decode(&doc)
	if err == mongo.ErrNoDocuments {
		return platform.JSON(req, 404, "Orden no encontrada.", nil), nil
	}
	if err != nil {
		return platform.Response{}, err
	}
	orden := platform.Doc(doc)
	renombrarSucursal(orden)

	// Datos frescos del vehículo en lugar del snapshot guardado al crear la OS.
	if vid, ok := orden["vehiculo_id"].(string); ok && len(vid) == 24 {
		if voi, err := bson.ObjectIDFromHex(vid); err == nil {
			var v bson.M
			err := db.Collection("vehiculos").FindOne(ctx, bson.D{{Key: "_id", Value: voi}}).Decode(&v)
			switch {
			case err == nil:
				vehiculo := platform.Doc(v)
				renombrarSucursal(vehiculo)
				orden["vehiculo_snapshot"] = vehiculo
			case err != mongo.ErrNoDocuments:
				// Como Python: si falla, se queda el snapshot guardado en la OS.
				platform.Logger().Warn("no se pudo leer el vehículo de la OS", "orden", oid.Hex(), "error", err)
			}
		}
	}

	if orden["estado"] == "COTIZADO" {
		n, err := db.Collection("cotizacion_acceso").CountDocuments(ctx,
			bson.D{{Key: "orden_id", Value: oid.Hex()}}, options.Count().SetLimit(1))
		if err != nil {
			return platform.Response{}, err
		}
		orden["cliente_link_enviado"] = n > 0
	}

	if platform.EsMecanico(claims) {
		quitarImportes(orden)
	}
	return platform.JSON(req, 200, "Orden obtenida", orden), nil
}

func renombrarSucursal(m map[string]any) {
	if v, ok := m["sucursal_id"]; ok {
		m["sucursalId"] = v
		delete(m, "sucursal_id")
	}
}
