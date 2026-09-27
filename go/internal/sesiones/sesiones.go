// Package sesiones limita las sesiones concurrentes por usuario (port de
// src/handlers/auth/sesiones_manager.py).
//
// Cognito no limita en cuántos dispositivos se firma un mismo usuario, así que el
// límite se lleva aquí: cada navegador manda un `device_id` estable y el backend
// mantiene el registro de sesiones vivas en `_platform.sesiones_usuario`.
//
// Al abrir una sesión nueva por encima del máximo se revoca la MÁS ANTIGUA (por
// `ultimo_acceso`): ese dispositivo lo descubre en su siguiente latido y cierra
// sesión solo. El token de Cognito del dispositivo revocado sigue siendo válido
// hasta que expire: este control desalienta compartir la cuenta, no sustituye
// una revocación dura de tokens.
package sesiones

import (
	"context"
	"strings"
	"sync"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	coleccion = "sesiones_usuario"

	// MaxSesiones es el número de sesiones simultáneas permitidas por usuario.
	MaxSesiones = 2

	// Sin latido en esta ventana la sesión deja de contar para el límite: evita
	// que un navegador cerrado sin logout bloquee a su dueño para siempre.
	ventanaInactividad = 12 * time.Hour

	// Los documentos se autolimpian con un índice TTL sobre `expira_en`.
	retencion = 7 * 24 * time.Hour

	motivoLimite = "limite_sesiones"
)

var (
	indicesMu     sync.Mutex
	indicesListos bool
)

func col(ctx context.Context) (*mongo.Collection, error) {
	db, err := platform.PlatformDB()
	if err != nil {
		return nil, err
	}
	c := db.Collection(coleccion)

	indicesMu.Lock()
	defer indicesMu.Unlock()
	if !indicesListos {
		_, err := c.Indexes().CreateMany(ctx, []mongo.IndexModel{
			{
				Keys:    bson.D{{Key: "user_sub", Value: 1}, {Key: "device_id", Value: 1}},
				Options: options.Index().SetUnique(true).SetName("uniq_user_device"),
			},
			{
				Keys:    bson.D{{Key: "expira_en", Value: 1}},
				Options: options.Index().SetExpireAfterSeconds(0).SetName("ttl_expira_en"),
			},
		})
		if err != nil {
			// No debe tumbar el login: se reintenta en la siguiente invocación.
			platform.Logger().Warn("no se pudieron asegurar índices de sesiones", "error", err)
		} else {
			indicesListos = true
		}
	}
	return c, nil
}

type identidad struct {
	sub, email, tenantID string
}

func identidadDe(req platform.Request) identidad {
	c := platform.Claims(req)
	return identidad{
		sub:      platform.ClaimString(c, "sub"),
		email:    platform.ClaimString(c, "email"),
		tenantID: platform.ClaimString(c, "custom:tenant_id"),
	}
}

type sesion struct {
	ID           bson.ObjectID `bson:"_id"`
	DeviceID     string        `bson:"device_id"`
	UserAgent    string        `bson:"user_agent"`
	UltimoAcceso time.Time     `bson:"ultimo_acceso"`
	RevocadaEn   *time.Time    `bson:"revocada_en"`
	Motivo       *string       `bson:"motivo"`
}

// vivas devuelve las sesiones no revocadas con latido dentro de la ventana, más
// reciente primero. El desempate por `createdAt` importa: dos pestañas pueden
// latir en el mismo milisegundo y sin él la sesión revocada sería arbitraria.
func vivas(ctx context.Context, c *mongo.Collection, sub string, ahora time.Time, excluir string) ([]sesion, error) {
	filtro := bson.D{
		{Key: "user_sub", Value: sub},
		{Key: "revocada_en", Value: nil},
		{Key: "ultimo_acceso", Value: bson.D{{Key: "$gte", Value: ahora.Add(-ventanaInactividad)}}},
	}
	if excluir != "" {
		filtro = append(filtro, bson.E{Key: "device_id", Value: bson.D{{Key: "$ne", Value: excluir}}})
	}
	cur, err := c.Find(ctx, filtro, options.Find().SetSort(bson.D{
		{Key: "ultimo_acceso", Value: -1}, {Key: "createdAt", Value: -1},
	}))
	if err != nil {
		return nil, err
	}
	var out []sesion
	if err := cur.All(ctx, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// sobrantes elige qué sesiones revocar: el dispositivo actual siempre se queda,
// así que del resto (ordenado de más a menos reciente) sobreviven MaxSesiones-1.
func sobrantes(otras []sesion) []sesion {
	if len(otras) <= MaxSesiones-1 {
		return nil
	}
	return otras[MaxSesiones-1:]
}

// registrar da de alta o refresca la sesión del dispositivo y poda las
// sobrantes. Devuelve (sesiones activas, sesiones revocadas).
func registrar(ctx context.Context, c *mongo.Collection, id identidad, deviceID, userAgent string, ahora time.Time) (int, int, error) {
	filtro := bson.D{{Key: "user_sub", Value: id.sub}, {Key: "device_id", Value: deviceID}}
	update := bson.D{
		{Key: "$set", Value: bson.D{
			{Key: "email", Value: id.email},
			{Key: "tenant_id", Value: id.tenantID},
			{Key: "user_agent", Value: userAgent},
			{Key: "ultimo_acceso", Value: ahora},
			{Key: "expira_en", Value: ahora.Add(retencion)},
			{Key: "revocada_en", Value: nil},
			{Key: "motivo", Value: nil},
		}},
		{Key: "$setOnInsert", Value: bson.D{{Key: "createdAt", Value: ahora}}},
	}
	_, err := c.UpdateOne(ctx, filtro, update, options.UpdateOne().SetUpsert(true))
	if mongo.IsDuplicateKeyError(err) {
		// Dos peticiones simultáneas del mismo dispositivo compiten por el upsert;
		// la perdedora choca con el índice único y al reintentar ya actualiza.
		_, err = c.UpdateOne(ctx, filtro, update, options.UpdateOne().SetUpsert(true))
	}
	if err != nil {
		return 0, 0, err
	}

	otras, err := vivas(ctx, c, id.sub, ahora, deviceID)
	if err != nil {
		return 0, 0, err
	}
	revocar := sobrantes(otras)
	for _, s := range revocar {
		_, err := c.UpdateOne(ctx, bson.D{{Key: "_id", Value: s.ID}}, bson.D{{Key: "$set", Value: bson.D{
			{Key: "revocada_en", Value: ahora},
			{Key: "motivo", Value: motivoLimite},
		}}})
		if err != nil {
			return 0, 0, err
		}
	}
	if len(revocar) > 0 {
		platform.Logger().Info("límite de sesiones: se revocaron sesiones",
			"max", MaxSesiones, "revocadas", len(revocar), "email", id.email)
	}

	activas := min(len(otras)-len(revocar)+1, MaxSesiones)
	return activas, len(revocar), nil
}

func deviceIDQuery(req platform.Request) string {
	return strings.TrimSpace(req.QueryStringParameters["device_id"])
}

// Registrar atiende POST /auth/sesiones: el front la llama justo después de un
// login exitoso.
func Registrar(ctx context.Context, req platform.Request) (platform.Response, error) {
	id := identidadDe(req)
	if id.sub == "" {
		return platform.JSON(req, 401, "Token sin identidad de usuario.", nil), nil
	}

	var body struct {
		DeviceID  any    `json:"device_id"`
		UserAgent string `json:"user_agent"`
	}
	if err := platform.ParseBody(req, &body); err != nil {
		return platform.Response{}, err
	}
	deviceID := deviceIDQuery(req)
	if s, ok := body.DeviceID.(string); ok && strings.TrimSpace(s) != "" {
		deviceID = strings.TrimSpace(s)
	}
	if deviceID == "" {
		return platform.JSON(req, 400, "El campo 'device_id' es obligatorio.", nil), nil
	}

	c, err := col(ctx)
	if err != nil {
		return platform.Response{}, err
	}
	ahora := time.Now().UTC()
	activas, revocadas, err := registrar(ctx, c, id, deviceID, platform.Truncar(body.UserAgent, 300), ahora)
	if err != nil {
		return platform.Response{}, err
	}

	return platform.JSON(req, 200, "Sesión registrada", map[string]any{
		"device_id":         deviceID,
		"sesiones_activas":  activas,
		"max_sesiones":      MaxSesiones,
		"sesiones_cerradas": revocadas,
	}), nil
}

// Estado atiende GET /auth/sesiones?device_id=…: el latido que dice si esta
// sesión sigue vigente.
//
// Un dispositivo sin registro (sesión abierta antes de que existiera el control)
// o cuyo último latido cayó fuera de la ventana se trata como un login nuevo:
// entra y, si hace falta, desplaza a la sesión más antigua. Antes el dispositivo
// dormido solo se refrescaba sin podar y el usuario podía quedar con tres sesiones.
func Estado(ctx context.Context, req platform.Request) (platform.Response, error) {
	id := identidadDe(req)
	if id.sub == "" {
		return platform.JSON(req, 401, "Token sin identidad de usuario.", nil), nil
	}
	deviceID := deviceIDQuery(req)
	if deviceID == "" {
		return platform.JSON(req, 400, "El parámetro 'device_id' es obligatorio.", nil), nil
	}

	c, err := col(ctx)
	if err != nil {
		return platform.Response{}, err
	}
	ahora := time.Now().UTC()

	var doc sesion
	err = c.FindOne(ctx, bson.D{{Key: "user_sub", Value: id.sub}, {Key: "device_id", Value: deviceID}}).Decode(&doc)
	existe := err == nil
	if err != nil && err != mongo.ErrNoDocuments {
		return platform.Response{}, err
	}

	if existe && doc.RevocadaEn != nil {
		motivo := motivoLimite
		if doc.Motivo != nil && *doc.Motivo != "" {
			motivo = *doc.Motivo
		}
		return platform.JSON(req, 200, "Sesión revocada", map[string]any{
			"vigente":      false,
			"motivo":       motivo,
			"max_sesiones": MaxSesiones,
		}), nil
	}

	if !existe || doc.UltimoAcceso.Before(ahora.Add(-ventanaInactividad)) {
		activas, _, err := registrar(ctx, c, id, deviceID, doc.UserAgent, ahora)
		if err != nil {
			return platform.Response{}, err
		}
		return platform.JSON(req, 200, "Sesión registrada", map[string]any{
			"vigente":          true,
			"sesiones_activas": activas,
			"max_sesiones":     MaxSesiones,
		}), nil
	}

	_, err = c.UpdateOne(ctx, bson.D{{Key: "_id", Value: doc.ID}}, bson.D{{Key: "$set", Value: bson.D{
		{Key: "ultimo_acceso", Value: ahora},
		{Key: "expira_en", Value: ahora.Add(retencion)},
	}}})
	if err != nil {
		return platform.Response{}, err
	}
	activas, err := vivas(ctx, c, id.sub, ahora, "")
	if err != nil {
		return platform.Response{}, err
	}

	return platform.JSON(req, 200, "Sesión vigente", map[string]any{
		"vigente":          true,
		"sesiones_activas": len(activas),
		"max_sesiones":     MaxSesiones,
	}), nil
}

// Cerrar atiende DELETE /auth/sesiones?device_id=…: libera el cupo al cerrar sesión.
func Cerrar(ctx context.Context, req platform.Request) (platform.Response, error) {
	id := identidadDe(req)
	if id.sub == "" {
		return platform.JSON(req, 401, "Token sin identidad de usuario.", nil), nil
	}
	deviceID := deviceIDQuery(req)
	if deviceID == "" {
		return platform.JSON(req, 400, "El parámetro 'device_id' es obligatorio.", nil), nil
	}

	c, err := col(ctx)
	if err != nil {
		return platform.Response{}, err
	}
	if _, err := c.DeleteOne(ctx, bson.D{{Key: "user_sub", Value: id.sub}, {Key: "device_id", Value: deviceID}}); err != nil {
		return platform.Response{}, err
	}
	return platform.JSON(req, 200, "Sesión cerrada", nil), nil
}
