// Package vehiculos atiende el detalle de un vehículo y la decodificación de VIN
// (port de get_vehiculo_handler y decode_vin_handler en
// src/handlers/vehiculos/vehiculos_manager.py). El listado y el CRUD siguen en
// Python.
package vehiculos

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
)

// Get atiende GET /vehiculos/{id}: el vehículo con el nombre de su dueño.
func Get(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
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

	cur, err := db.Collection("vehiculos").Aggregate(ctx, pipelineDetalle(oid))
	if err != nil {
		return platform.Response{}, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return platform.Response{}, err
	}
	if len(filas) == 0 {
		return platform.JSON(req, 404, "Vehículo no encontrado.", nil), nil
	}

	v := platform.Doc(filas[0])
	if s, ok := v["sucursal_id"]; ok {
		v["sucursalId"] = s
		delete(v, "sucursal_id")
	}
	// Documentos viejos guardaban el año como 'año'; manda 'anio' si existe.
	if anio, ok := v["año"]; ok {
		if _, hay := v["anio"]; !hay {
			v["anio"] = anio
		}
		delete(v, "año")
	}
	return platform.JSON(req, 200, "Vehículo obtenido", v), nil
}

// pipelineDetalle es el mismo aggregate de Python: cliente_id se guarda como
// texto, así que se convierte a ObjectId para el $lookup, y el nombre queda
// "nombre paterno materno" o "Cliente Desconocido".
func pipelineDetalle(oid bson.ObjectID) mongo.Pipeline {
	primero := func(campo string) bson.D {
		return bson.D{{Key: "$ifNull", Value: bson.A{
			bson.D{{Key: "$arrayElemAt", Value: bson.A{"$cliente_info." + campo, 0}}}, "",
		}}}
	}
	return mongo.Pipeline{
		{{Key: "$match", Value: bson.D{{Key: "_id", Value: oid}}}},
		{{Key: "$addFields", Value: bson.D{{Key: "cliente_oid", Value: bson.D{{Key: "$convert", Value: bson.D{
			{Key: "input", Value: "$cliente_id"},
			{Key: "to", Value: "objectId"},
			{Key: "onError", Value: "$cliente_id"},
			{Key: "onNull", Value: nil},
		}}}}}}},
		{{Key: "$lookup", Value: bson.D{
			{Key: "from", Value: "clientes"},
			{Key: "localField", Value: "cliente_oid"},
			{Key: "foreignField", Value: "_id"},
			{Key: "as", Value: "cliente_info"},
		}}},
		{{Key: "$addFields", Value: bson.D{{Key: "cliente_nombre", Value: bson.D{{Key: "$cond", Value: bson.D{
			{Key: "if", Value: bson.D{{Key: "$gt", Value: bson.A{bson.D{{Key: "$size", Value: "$cliente_info"}}, 0}}}},
			{Key: "then", Value: bson.D{{Key: "$concat", Value: bson.A{
				primero("nombre"), " ", primero("apellido_paterno"), " ", primero("apellido_materno"),
			}}}},
			{Key: "else", Value: "Cliente Desconocido"},
		}}}}}}},
		{{Key: "$project", Value: bson.D{{Key: "cliente_info", Value: 0}, {Key: "cliente_oid", Value: 0}}}},
	}
}

const longitudVIN = 17

// nhtsaURL y httpClient son reemplazables en pruebas.
var (
	nhtsaURL   = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/"
	httpClient = &http.Client{Timeout: 10 * time.Second}
)

// DecodeVIN atiende GET /vehiculos/decode-vin/{vin}: consulta la API pública de
// la NHTSA y devuelve su respuesta tal cual.
func DecodeVIN(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	vin := strings.TrimSpace(req.PathParameters["vin"])
	if utf8.RuneCountInString(vin) != longitudVIN {
		return platform.JSON(req, 400, "El VIN debe tener exactamente 17 caracteres.", nil), nil
	}

	data, err := consultarNHTSA(ctx, vin)
	if err != nil {
		// Python solo respondía 502 a errores de red; un timeout de lectura o un
		// JSON inválido salían como 500/400. Todo fallo del servicio externo es 502.
		platform.Logger().Error("Error al consultar la API de la NHTSA", "error", err)
		return platform.JSON(req, 502, "Error al conectar con el servicio externo de la NHTSA.", nil), nil
	}
	return platform.JSON(req, 200, "VIN decodificado exitosamente", data), nil
}

func consultarNHTSA(ctx context.Context, vin string) (json.RawMessage, error) {
	u := nhtsaURL + url.PathEscape(vin) + "?format=json"
	hreq, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	hreq.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
	resp, err := httpClient.Do(hreq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, fmt.Errorf("NHTSA respondió %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if !json.Valid(body) {
		return nil, fmt.Errorf("NHTSA devolvió un cuerpo que no es JSON")
	}
	return json.RawMessage(body), nil
}
