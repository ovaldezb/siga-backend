// Package autorizaciones atiende las solicitudes que el POS manda a los
// administradores (p. ej. un precio especial) y su resolución. Port de
// src/handlers/ventas/autorizaciones_manager.py.
package autorizaciones

import (
	"context"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	coleccion = "autorizaciones"

	Pendiente = "PENDIENTE"
	Aprobada  = "APROBADA"
	Rechazada = "RECHAZADA"

	tipoPrecioPOS = "PRECIO_POS"
	limiteLista   = 50
)

var estadosValidos = map[string]bool{Pendiente: true, Aprobada: true, Rechazada: true}

func tenantDe(req platform.Request) (map[string]any, string) {
	c := platform.Claims(req)
	return c, platform.ClaimString(c, "custom:tenant_id")
}

// nombreDe usa el nombre del token y cae al email: muchos usuarios de Cognito no
// traen el claim `name` y quedaban como "Usuario Desconocido".
func nombreDe(claims map[string]any, porDefecto string) string {
	if n := strings.TrimSpace(platform.ClaimString(claims, "name")); n != "" {
		return n
	}
	if e := strings.TrimSpace(platform.ClaimString(claims, "email")); e != "" {
		return e
	}
	return porDefecto
}

// Create atiende POST /autorizaciones.
func Create(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims, tenantID := tenantDe(req)
	if tenantID == "" {
		return platform.JSON(req, 403, "No tenantId", nil), nil
	}

	var body struct {
		SucursalID string         `json:"sucursal_id"`
		Tipo       string         `json:"tipo"`
		Metadata   map[string]any `json:"metadata"`
	}
	if err := platform.ParseBody(req, &body); err != nil {
		return platform.Response{}, err
	}
	body.SucursalID = strings.TrimSpace(body.SucursalID)
	if body.SucursalID == "" {
		return platform.JSON(req, 400, "El campo 'sucursal_id' es obligatorio.", nil), nil
	}
	if body.Tipo == "" {
		body.Tipo = tipoPrecioPOS
	}
	if body.Metadata == nil {
		body.Metadata = map[string]any{}
	}
	// El POS aplica metadata.precio_solicitado al aprobarse: sin un número válido
	// la autorización se aprobaba y el carrito quedaba con precio NaN/undefined.
	if body.Tipo == tipoPrecioPOS {
		precio, ok := body.Metadata["precio_solicitado"].(float64)
		if !ok || precio < 0 {
			return platform.JSON(req, 400, "El precio solicitado debe ser un número mayor o igual a cero.", nil), nil
		}
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	ahora := time.Now().UTC()
	solicitante := bson.M{
		"id":     platform.ClaimString(claims, "sub"),
		"nombre": nombreDe(claims, "Usuario Desconocido"),
	}
	doc := bson.D{
		{Key: "tenant_id", Value: tenantID},
		{Key: "sucursal_id", Value: body.SucursalID},
		{Key: "tipo", Value: body.Tipo},
		{Key: "estado", Value: Pendiente},
		{Key: "solicitante", Value: solicitante},
		{Key: "metadata", Value: body.Metadata},
		{Key: "createdAt", Value: ahora},
	}
	res, err := db.Collection(coleccion).InsertOne(ctx, doc)
	if err != nil {
		return platform.Response{}, err
	}

	oid, _ := res.InsertedID.(bson.ObjectID)
	return platform.JSON(req, 201, "Autorización solicitada", map[string]any{
		"id":          oid.Hex(),
		"tenant_id":   tenantID,
		"sucursal_id": body.SucursalID,
		"tipo":        body.Tipo,
		"estado":      Pendiente,
		"solicitante": solicitante,
		"metadata":    body.Metadata,
		"createdAt":   platform.IsoUTC(ahora),
	}), nil
}

// estadosFiltro acepta un estado o varios separados por coma
// (`estado=APROBADA,RECHAZADA`), para que el POS se entere también del rechazo.
func estadosFiltro(raw string) ([]string, error) {
	if strings.TrimSpace(raw) == "" {
		return []string{Pendiente}, nil
	}
	var out []string
	for e := range strings.SplitSeq(raw, ",") {
		e = strings.ToUpper(strings.TrimSpace(e))
		if e == "" {
			continue
		}
		if !estadosValidos[e] {
			return nil, platform.BadRequest("estado '%s' no válido", e)
		}
		out = append(out, e)
	}
	if len(out) == 0 {
		return []string{Pendiente}, nil
	}
	return out, nil
}

// List atiende GET /autorizaciones?estado=&sucursal_id=.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	_, tenantID := tenantDe(req)
	if tenantID == "" {
		return platform.JSON(req, 403, "No tenantId", nil), nil
	}

	estados, err := estadosFiltro(req.QueryStringParameters["estado"])
	if err != nil {
		return platform.Response{}, err
	}
	filtro := bson.D{
		{Key: "tenant_id", Value: tenantID},
		{Key: "estado", Value: bson.D{{Key: "$in", Value: estados}}},
	}
	if s := req.QueryStringParameters["sucursal_id"]; s != "" {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: s})
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := db.Collection(coleccion).Find(ctx, filtro, options.Find().
		SetSort(bson.D{{Key: "createdAt", Value: -1}}).
		SetLimit(limiteLista))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}

	items := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		items = append(items, platform.Doc(d))
	}
	return platform.JSON(req, 200, "Autorizaciones", map[string]any{"items": items}), nil
}

// Update atiende PUT /autorizaciones/{id}: aprueba o rechaza una pendiente.
// Solo ADMIN/SUPER_ADMIN: antes cualquier usuario del taller (el propio cajero
// que la pidió) podía aprobarse el precio especial llamando al endpoint.
func Update(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims, tenantID := tenantDe(req)
	if tenantID == "" {
		return platform.JSON(req, 403, "No tenantId", nil), nil
	}
	if !platform.IsAdmin(claims) {
		return platform.JSON(req, 403, "Solo un administrador puede resolver autorizaciones.", nil), nil
	}

	oid, err := platform.ParseObjectID(req.PathParameters["id"], "id de autorización")
	if err != nil {
		return platform.Response{}, err
	}
	var body struct {
		Estado string `json:"estado"`
	}
	if err := platform.ParseBody(req, &body); err != nil {
		return platform.Response{}, err
	}
	if body.Estado != Aprobada && body.Estado != Rechazada {
		return platform.JSON(req, 400, "Estado inválido", nil), nil
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	var result bson.M
	err = db.Collection(coleccion).FindOneAndUpdate(ctx,
		bson.D{{Key: "_id", Value: oid}, {Key: "tenant_id", Value: tenantID}, {Key: "estado", Value: Pendiente}},
		bson.D{{Key: "$set", Value: bson.D{
			{Key: "estado", Value: body.Estado},
			{Key: "aprobador", Value: bson.D{
				{Key: "id", Value: platform.ClaimString(claims, "sub")},
				{Key: "nombre", Value: nombreDe(claims, "Admin")},
			}},
			{Key: "updatedAt", Value: time.Now().UTC()},
		}}},
		options.FindOneAndUpdate().SetReturnDocument(options.After),
	).Decode(&result)
	if err == mongo.ErrNoDocuments {
		return platform.JSON(req, 404, "Autorización no encontrada o ya resuelta.", nil), nil
	}
	if err != nil {
		return platform.Response{}, err
	}

	return platform.JSON(req, 200, "Autorización actualizada", platform.Doc(result)), nil
}
