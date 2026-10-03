// Package detalle atiende los GET /<recurso>/{id} que solo leen un documento del
// tenant y lo devuelven tal cual (port de get_proveedor_handler, get_cita_handler,
// get_compra_handler y get_cotizacion_handler). Cada recurso conserva los
// mensajes y el filtro de su handler de Python; el resto de su CRUD sigue allá.
package detalle

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
)

// Recurso describe un GET por id: la colección y los textos de respuesta.
type Recurso struct {
	Coleccion    string
	SinTenant    string
	IDInvalido   string
	NoEncontrado string
	Encontrado   string
	FiltraTenant bool // cotizaciones además filtra por tenant_id dentro de su base
}

var (
	Proveedor = Recurso{
		Coleccion:    "proveedores",
		SinTenant:    "No se encontró un tenantId asociado.",
		IDInvalido:   "ID de proveedor inválido.",
		NoEncontrado: "Proveedor no encontrado.",
		Encontrado:   "Detalle del proveedor",
	}
	Cita = Recurso{
		Coleccion:    "citas",
		SinTenant:    "No se encontró un tenantId asociado.",
		IDInvalido:   "ID inválido.",
		NoEncontrado: "Cita no encontrada.",
		Encontrado:   "Detalle de la cita",
	}
	Compra = Recurso{
		Coleccion:    "compras",
		SinTenant:    "No autorizado",
		IDInvalido:   "ID inválido.",
		NoEncontrado: "Compra no encontrada.",
		Encontrado:   "Detalle de compra",
	}
	Cotizacion = Recurso{
		Coleccion:    "cotizaciones",
		SinTenant:    "No se encontró un tenantId asociado.",
		IDInvalido:   "ID inválido.",
		NoEncontrado: "Cotización no encontrada",
		Encontrado:   "Cotización obtenida",
		FiltraTenant: true,
	}
)

// Handler devuelve la lambda del recurso.
func (r Recurso) Handler() platform.Handler {
	return func(ctx context.Context, req platform.Request) (platform.Response, error) {
		tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
		if tenantID == "" {
			return platform.JSON(req, 403, r.SinTenant, nil), nil
		}
		oid, err := bson.ObjectIDFromHex(req.PathParameters["id"])
		if err != nil {
			return platform.JSON(req, 400, r.IDInvalido, nil), nil
		}
		db, err := platform.TenantDB(tenantID)
		if err != nil {
			return platform.Response{}, err
		}

		filtro := bson.D{{Key: "_id", Value: oid}}
		if r.FiltraTenant {
			filtro = append(filtro, bson.E{Key: "tenant_id", Value: tenantID})
		}
		var doc bson.M
		err = db.Collection(r.Coleccion).FindOne(ctx, filtro).Decode(&doc)
		if err == mongo.ErrNoDocuments {
			return platform.JSON(req, 404, r.NoEncontrado, nil), nil
		}
		if err != nil {
			return platform.Response{}, err
		}
		return platform.JSON(req, 200, r.Encontrado, platform.Doc(doc)), nil
	}
}
