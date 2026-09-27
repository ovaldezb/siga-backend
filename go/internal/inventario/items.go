package inventario

import (
	"context"
	"regexp"
	"sort"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	limiteItems         = 50
	maxTerminosBusqueda = 6
	tipoServicio        = "SERVICIO"
)

var camposBusqueda = []string{"nombre", "no_parte", "marca", "proveedor", "categoria", "descripcion"}

// Items atiende GET /items (port de list_items_handler). Filtros:
//   - sucursalId / sucursal_id, con el scope de sucursal del usuario.
//   - tipo.
//   - soloInventario=true (POS): excluye los productos maneja_inventario:false,
//     que son capturas manuales de las OS; los servicios se conservan.
//   - search: cada palabra (hasta 6) debe aparecer en algún campo.
//   - existencias=true: agrega el stock del mismo número de parte en todas las
//     sucursales ("¿lo hay en la otra sucursal?").
func Items(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		// Python no lo validaba y get_tenant_db respondía 400.
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	qp := req.QueryStringParameters
	pedida := qp["sucursalId"]
	if pedida == "" {
		pedida = qp["sucursal_id"]
	}
	soloInventario := esTrue(qp, "soloInventario", "solo_inventario")
	conExistencias := esTrue(qp, "existencias", "incluirExistencias")
	page, limit, skip, err := platform.Paginacion(qp, limiteItems)
	if err != nil {
		return platform.Response{}, err
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	scope, violacion, err := platform.SucursalScope(ctx, claims, db, pedida)
	if err != nil {
		return platform.Response{}, err
	}
	if violacion != "" {
		return platform.JSON(req, 403, violacion, nil), nil
	}

	filtro := bson.D{}
	var y bson.A
	if scope != nil {
		y = append(y, bson.D{{Key: "sucursal_id", Value: platform.FiltroSucursal(scope)}})
	}
	if tipo := qp["tipo"]; tipo != "" {
		filtro = append(filtro, bson.E{Key: "tipo", Value: tipo})
	}
	if soloInventario {
		y = append(y, bson.D{{Key: "$or", Value: bson.A{
			bson.D{{Key: "tipo", Value: tipoServicio}},
			bson.D{{Key: "maneja_inventario", Value: bson.D{{Key: "$ne", Value: false}}}},
		}}})
	}
	y = append(y, condicionesBusqueda(qp["search"])...)
	if len(y) > 0 {
		filtro = append(filtro, bson.E{Key: "$and", Value: y})
	}

	col := db.Collection("items")
	total, err := col.CountDocuments(ctx, filtro)
	if err != nil {
		return platform.Response{}, err
	}
	// Sin orden explícito, como Python: el orden natural de la colección.
	cur, err := col.Find(ctx, filtro, options.Find().SetSkip(skip).SetLimit(limit))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	items := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		i := platform.Doc(d)
		if v, ok := i["sucursal_id"]; ok {
			i["sucursalId"] = v
			delete(i, "sucursal_id")
		}
		items = append(items, i)
	}

	var sucursales []sucursal
	if conExistencias {
		existencias, err := existenciasPorSucursal(ctx, col, items)
		if err != nil {
			return platform.Response{}, err
		}
		if sucursales, err = sucursalesTenant(ctx, db); err != nil {
			return platform.Response{}, err
		}
		for _, i := range items {
			if inventariable(i) {
				agregarExistencias(i, existencias[claveNoParte(i)], sucursales)
			}
		}
	}

	return platform.JSON(req, 200, "Items obtenidos", map[string]any{
		"items":      items,
		"total":      total,
		"page":       page,
		"limit":      limit,
		"sucursales": sucursales, // null si no se pidieron existencias
	}), nil
}

func esTrue(qp map[string]string, claves ...string) bool {
	for _, k := range claves {
		if v := qp[k]; v != "" {
			return strings.ToLower(v) == "true"
		}
	}
	return false
}

// condicionesBusqueda: una condición por palabra (AND entre ellas) para que el
// segundo término acote en vez de ampliar. Cada término va escapado.
func condicionesBusqueda(search string) bson.A {
	terminos := strings.Fields(search)
	if len(terminos) > maxTerminosBusqueda {
		terminos = terminos[:maxTerminosBusqueda]
	}
	out := make(bson.A, 0, len(terminos))
	for _, t := range terminos {
		re := bson.Regex{Pattern: regexp.QuoteMeta(t), Options: "i"}
		or := make(bson.A, 0, len(camposBusqueda))
		for _, campo := range camposBusqueda {
			or = append(or, bson.D{{Key: campo, Value: re}})
		}
		out = append(out, bson.D{{Key: "$or", Value: or}})
	}
	return out
}

// inventariable: lleva existencia física (no es servicio ni captura manual).
func inventariable(i map[string]any) bool {
	if i["tipo"] == tipoServicio {
		return false
	}
	v, ok := i["maneja_inventario"]
	return !ok || platform.Verdadero(v)
}

func noParte(i map[string]any) string {
	s, _ := i["no_parte"].(string)
	return strings.TrimSpace(s)
}

func claveNoParte(i map[string]any) string { return strings.ToLower(noParte(i)) }

type sucursal struct {
	ID     string `json:"id"`
	Nombre string `json:"nombre"`
	Activa any    `json:"activa"`
}

// sucursalesTenant: [{id, nombre, activa}] ordenadas por nombre.
func sucursalesTenant(ctx context.Context, db *mongo.Database) ([]sucursal, error) {
	cur, err := db.Collection("sucursales").Find(ctx, bson.D{},
		options.Find().SetProjection(bson.D{{Key: "nombre", Value: 1}, {Key: "activa", Value: 1}}))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	out := make([]sucursal, 0, len(docs))
	for _, d := range docs {
		s := platform.Doc(d)
		id, _ := s["id"].(string)
		nombre, _ := s["nombre"].(string)
		if nombre == "" {
			nombre = "(sin nombre)"
		}
		activa, ok := s["activa"]
		if !ok {
			activa = true
		}
		out = append(out, sucursal{ID: id, Nombre: nombre, Activa: activa})
	}
	sort.SliceStable(out, func(a, b int) bool { return out[a].Nombre < out[b].Nombre })
	return out, nil
}

// existenciasPorSucursal: {no_parte en minúsculas: {sucursal_id: stock}} para
// los artículos de la página, en una sola agregación. Se buscan las variantes
// exactas de mayúsculas con $in (no $regex) para que use el índice.
func existenciasPorSucursal(ctx context.Context, col *mongo.Collection, items []map[string]any) (map[string]map[string]any, error) {
	vistas := map[string]bool{}
	var variantes []string
	for _, i := range items {
		np := noParte(i)
		if np == "" || !inventariable(i) {
			continue
		}
		for _, v := range []string{np, strings.ToUpper(np), strings.ToLower(np)} {
			if !vistas[v] {
				vistas[v] = true
				variantes = append(variantes, v)
			}
		}
	}
	out := map[string]map[string]any{}
	if len(variantes) == 0 {
		return out, nil
	}

	cur, err := col.Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: bson.D{
			{Key: "no_parte", Value: bson.D{{Key: "$in", Value: variantes}}},
			{Key: "tipo", Value: bson.D{{Key: "$ne", Value: tipoServicio}}},
		}}},
		{{Key: "$group", Value: bson.D{
			{Key: "_id", Value: bson.D{
				{Key: "np", Value: bson.D{{Key: "$toLower", Value: "$no_parte"}}},
				{Key: "sucursal", Value: "$sucursal_id"},
			}},
			{Key: "stock", Value: bson.D{{Key: "$sum", Value: bson.D{{Key: "$ifNull", Value: bson.A{"$stock", 0}}}}}},
		}}},
	})
	if err != nil {
		return nil, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return nil, err
	}
	for _, f := range filas {
		clave, _ := platform.Doc(f)["id"].(map[string]any) // Doc renombra _id → id
		np, _ := clave["np"].(string)
		suc, _ := clave["sucursal"].(string)
		if np == "" || suc == "" {
			continue
		}
		if out[np] == nil {
			out[np] = map[string]any{}
		}
		stock := f["stock"]
		if !platform.Verdadero(stock) {
			stock = 0
		}
		out[np][suc] = stock
	}
	return out, nil
}

// agregarExistencias pone en el artículo el stock por sucursal (las activas y
// cualquier inactiva que aún tenga stock) y su total.
func agregarExistencias(i map[string]any, porSucursal map[string]any, sucursales []sucursal) {
	actual, _ := i["sucursalId"].(string)
	existencias := make([]map[string]any, 0, len(sucursales))
	total := 0.0
	for _, s := range sucursales {
		stock, ok := porSucursal[s.ID]
		if !ok {
			stock = 0
		}
		if !platform.Verdadero(s.Activa) && !platform.Verdadero(stock) {
			continue
		}
		total += platform.Numero(stock)
		existencias = append(existencias, map[string]any{
			"sucursal_id":     s.ID,
			"sucursal_nombre": s.Nombre,
			"stock":           stock,
			"es_actual":       s.ID == actual,
		})
	}
	i["existencias"] = existencias
	i["stock_total"] = total
}
