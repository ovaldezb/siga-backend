package clientes

import (
	"context"
	"maps"
	"math"
	"sort"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	maxCoincidencias = 10
	maxGrupos        = 300
)

// Duplicados atiende GET /clientes/duplicados en dos modos:
//
//   - Con datos (`telefono`, `email`, `rfc`, `placas`, `nombre` +
//     `apellido_paterno` [+ `apellido_materno`], opcional `excluir`): los clientes
//     del taller que ya tienen alguno de esos datos, de cualquier sucursal. El
//     front lo consulta antes de dar de alta para ofrecer el que ya existe.
//   - Sin datos (sólo ADMIN): grupos de clientes que parecen la misma persona,
//     para limpiarlos con POST /clientes/{id}/fusionar.
//
// Compara en memoria con los datos normalizados (ver identidad.go): un taller
// tiene a lo más unos miles de clientes y así "55 1234-5678" y "+525512345678"
// coinciden sin depender de cómo se capturó cada uno.
func Duplicados(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	qp := req.QueryStringParameters
	buscado := map[string]any{
		"nombre":           strings.TrimSpace(qp["nombre"]),
		"apellido_paterno": strings.TrimSpace(qp["apellido_paterno"]),
		"apellido_materno": strings.TrimSpace(qp["apellido_materno"]),
	}
	claves := map[string]string{
		PorTelefono: telefonoClave(qp["telefono"]),
		PorEmail:    emailClave(qp["email"]),
		PorRFC:      rfcClave(qp["rfc"]),
		PorPlacas:   placasClave(qp["placas"]),
		PorNombre:   nombreClave(buscado),
	}
	hayDatos := false
	for _, v := range claves {
		hayDatos = hayDatos || v != ""
	}
	sinCriterios := qp["telefono"] == "" && qp["email"] == "" && qp["rfc"] == "" &&
		qp["placas"] == "" && qp["nombre"] == "" && qp["apellido_paterno"] == ""
	if !hayDatos && !sinCriterios {
		// Se mandaron datos, pero ninguno identifica (teléfono incompleto, RFC genérico…).
		return platform.JSON(req, 200, "Sin coincidencias", map[string]any{"items": []any{}}), nil
	}
	if !hayDatos && !platform.IsAdmin(claims) {
		return platform.JSON(req, 403, "Sólo un administrador puede revisar los clientes duplicados.", nil), nil
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	cartera, err := cargarCartera(ctx, db)
	if err != nil {
		return platform.Response{}, err
	}
	sucursales, err := nombresSucursales(ctx, db)
	if err != nil {
		return platform.Response{}, err
	}

	if hayDatos {
		items := cartera.coincidencias(claves, qp["excluir"])
		conNombreSucursal(items, sucursales)
		return platform.JSON(req, 200, "Coincidencias", map[string]any{"items": items}), nil
	}

	grupos := cartera.grupos()
	ids := []string{}
	for _, g := range grupos {
		for _, c := range g.clientes {
			ids = append(ids, texto(c["id"]))
		}
	}
	if err := agregarMovimientos(ctx, db, grupos, ids); err != nil {
		return platform.Response{}, err
	}
	salida := make([]map[string]any, 0, len(grupos))
	for _, g := range grupos {
		conNombreSucursal(g.clientes, sucursales)
		salida = append(salida, map[string]any{"coincide_por": g.motivos, "clientes": g.clientes})
	}
	return platform.JSON(req, 200, "Posibles clientes duplicados", map[string]any{
		"grupos": salida,
		"total":  len(salida),
	}), nil
}

type cartera struct {
	clientes []map[string]any
	// placas normalizadas de los vehículos de cada cliente
	placas map[string][]string
	// claves "motivo|clave" de teléfonos/emails de relleno (ver marcarRelleno)
	relleno map[string]bool
}

// minPersonasRelleno: un teléfono o email que comparten tantas personas
// distintas no identifica a nadie. Pasa con el teléfono del propio taller,
// capturado a falta del del cliente (en prod unía a 9 clientes distintos en
// un solo grupo), o con el de una empresa y sus choferes.
const minPersonasRelleno = 3

// marcarRelleno detecta esos teléfonos/emails: los compartidos por al menos
// minPersonasRelleno personas con nombre distinto. Dos capturas de la misma
// persona (mismo nombre normalizado) cuentan como una.
func (c *cartera) marcarRelleno() {
	personas := map[string]map[string]bool{}
	for _, cl := range c.clientes {
		persona := nombreClave(cl)
		if persona == "" {
			persona = "id:" + texto(cl["id"])
		}
		for motivo, clave := range map[string]string{
			PorTelefono: telefonoClave(texto(cl["telefono"])),
			PorEmail:    emailClave(texto(cl["email"])),
		} {
			if clave == "" {
				continue
			}
			llave := motivo + "|" + clave
			if personas[llave] == nil {
				personas[llave] = map[string]bool{}
			}
			personas[llave][persona] = true
		}
	}
	for llave, ps := range personas {
		if len(ps) >= minPersonasRelleno {
			c.relleno[llave] = true
		}
	}
}

func cargarCartera(ctx context.Context, db *mongo.Database) (*cartera, error) {
	cur, err := db.Collection("clientes").Find(ctx, bson.D{}, options.Find().
		SetProjection(bson.D{
			{Key: "nombre", Value: 1}, {Key: "apellido_paterno", Value: 1}, {Key: "apellido_materno", Value: 1},
			{Key: "razon_social", Value: 1}, {Key: "telefono", Value: 1}, {Key: "email", Value: 1},
			{Key: "rfc", Value: 1}, {Key: "sucursal_id", Value: 1}, {Key: "createdAt", Value: 1},
			{Key: "flotilla_id", Value: 1}, {Key: "limite_credito", Value: 1},
		}).
		SetSort(bson.D{{Key: "_id", Value: 1}}))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	c := &cartera{placas: map[string][]string{}, relleno: map[string]bool{}}
	for _, d := range docs {
		cl := platform.Doc(d)
		if v, ok := cl["sucursal_id"]; ok {
			cl["sucursalId"] = v
			delete(cl, "sucursal_id")
		}
		c.clientes = append(c.clientes, cl)
	}
	c.marcarRelleno()

	vcur, err := db.Collection("vehiculos").Find(ctx,
		bson.D{{Key: "cliente_id", Value: bson.D{{Key: "$type", Value: "string"}}}},
		options.Find().SetProjection(bson.D{{Key: "cliente_id", Value: 1}, {Key: "placas", Value: 1}}))
	if err != nil {
		return nil, err
	}
	var vehs []bson.M
	if err := vcur.All(ctx, &vehs); err != nil {
		return nil, err
	}
	for _, v := range vehs {
		cid := texto(v["cliente_id"])
		c.placas[cid] = append(c.placas[cid], placasClave(texto(v["placas"])))
	}
	return c, nil
}

// clavesDe devuelve, por motivo, las claves que identifican al cliente.
func (c *cartera) clavesDe(cl map[string]any) map[string][]string {
	out := map[string][]string{}
	agregar := func(motivo, clave string) {
		if clave != "" && !c.relleno[motivo+"|"+clave] {
			out[motivo] = append(out[motivo], clave)
		}
	}
	agregar(PorTelefono, telefonoClave(texto(cl["telefono"])))
	agregar(PorEmail, emailClave(texto(cl["email"])))
	agregar(PorRFC, rfcClave(texto(cl["rfc"])))
	agregar(PorNombre, nombreClave(cl))
	for _, p := range c.placas[texto(cl["id"])] {
		agregar(PorPlacas, p)
	}
	return out
}

func (c *cartera) resumen(cl map[string]any) map[string]any {
	out := make(map[string]any, len(cl)+2)
	maps.Copy(out, cl)
	out["num_vehiculos"] = len(c.placas[texto(cl["id"])])
	return out
}

var ordenMotivos = []string{PorTelefono, PorEmail, PorRFC, PorPlacas, PorNombre}

func ordenarMotivos(set map[string]bool) []string {
	out := []string{}
	for _, m := range ordenMotivos {
		if set[m] {
			out = append(out, m)
		}
	}
	return out
}

func (c *cartera) coincidencias(buscadas map[string]string, excluir string) []map[string]any {
	type hallazgo struct {
		cl      map[string]any
		motivos []string
	}
	var hallados []hallazgo
	for _, cl := range c.clientes {
		if excluir != "" && texto(cl["id"]) == excluir {
			continue
		}
		set := map[string]bool{}
		for motivo, claves := range c.clavesDe(cl) {
			for _, k := range claves {
				if buscadas[motivo] != "" && k == buscadas[motivo] {
					set[motivo] = true
				}
			}
		}
		if len(set) > 0 {
			hallados = append(hallados, hallazgo{cl, ordenarMotivos(set)})
		}
	}
	// Primero quien coincide en más datos y, a igualdad, en el más confiable.
	peso := func(ms []string) int {
		p := 0
		for _, m := range ms {
			for i, o := range ordenMotivos {
				if m == o {
					p += 10 - i
				}
			}
		}
		return p
	}
	sort.SliceStable(hallados, func(i, j int) bool { return peso(hallados[i].motivos) > peso(hallados[j].motivos) })
	if len(hallados) > maxCoincidencias {
		hallados = hallados[:maxCoincidencias]
	}
	out := make([]map[string]any, 0, len(hallados))
	for _, h := range hallados {
		r := c.resumen(h.cl)
		r["coincide_por"] = h.motivos
		out = append(out, r)
	}
	return out
}

type grupo struct {
	motivos  []string
	clientes []map[string]any
}

// grupos une (union-find) a los clientes que comparten cualquier clave: si A y B
// comparten teléfono y B y C las placas, los tres son el mismo grupo.
func (c *cartera) grupos() []grupo {
	n := len(c.clientes)
	padre := make([]int, n)
	for i := range padre {
		padre[i] = i
	}
	var raiz func(int) int
	raiz = func(i int) int {
		for padre[i] != i {
			padre[i] = padre[padre[i]]
			i = padre[i]
		}
		return i
	}

	type cubeta struct {
		motivo   string
		miembros []int
	}
	cubetas := map[string]*cubeta{}
	for i, cl := range c.clientes {
		for motivo, claves := range c.clavesDe(cl) {
			for _, k := range claves {
				llave := motivo + "|" + k
				cb := cubetas[llave]
				if cb == nil {
					cb = &cubeta{motivo: motivo}
					cubetas[llave] = cb
				}
				if len(cb.miembros) == 0 || cb.miembros[len(cb.miembros)-1] != i {
					cb.miembros = append(cb.miembros, i)
				}
			}
		}
	}
	for _, cb := range cubetas {
		for _, m := range cb.miembros[1:] {
			padre[raiz(m)] = raiz(cb.miembros[0])
		}
	}
	motivos := map[int]map[string]bool{}
	for _, cb := range cubetas {
		if len(cb.miembros) < 2 {
			continue
		}
		r := raiz(cb.miembros[0])
		if motivos[r] == nil {
			motivos[r] = map[string]bool{}
		}
		motivos[r][cb.motivo] = true
	}

	miembros := map[int][]int{}
	raices := []int{}
	for i := range n {
		r := raiz(i)
		if motivos[r] == nil {
			continue
		}
		if _, ok := miembros[r]; !ok {
			raices = append(raices, r)
		}
		miembros[r] = append(miembros[r], i)
	}
	out := make([]grupo, 0, len(raices))
	for _, r := range raices {
		g := grupo{motivos: ordenarMotivos(motivos[r])}
		for _, i := range miembros[r] {
			g.clientes = append(g.clientes, c.resumen(c.clientes[i]))
		}
		out = append(out, g)
	}
	// Los más confiables primero: un teléfono compartido pesa más que un nombre.
	sort.SliceStable(out, func(i, j int) bool {
		return indiceMotivo(out[i].motivos[0]) < indiceMotivo(out[j].motivos[0])
	})
	if len(out) > maxGrupos {
		out = out[:maxGrupos]
	}
	return out
}

func indiceMotivo(m string) int {
	for i, o := range ordenMotivos {
		if o == m {
			return i
		}
	}
	return len(ordenMotivos)
}

// agregarMovimientos suma a cada cliente de los grupos sus OS, ventas y saldo
// de crédito: con eso el admin decide cuál registro conservar.
func agregarMovimientos(ctx context.Context, db *mongo.Database, grupos []grupo, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	enIDs := bson.D{{Key: "$in", Value: ids}}
	ordenes, err := contarPor(ctx, db.Collection("ordenes_servicio"),
		bson.D{{Key: "cliente_snapshot.id", Value: enIDs}}, "$cliente_snapshot.id", 1)
	if err != nil {
		return err
	}
	ventas, err := contarPor(ctx, db.Collection("ventas"),
		bson.D{{Key: "cliente_id", Value: enIDs}}, "$cliente_id", 1)
	if err != nil {
		return err
	}
	saldos, err := contarPor(ctx, db.Collection("ventas"), bson.D{
		{Key: "cliente_id", Value: enIDs},
		{Key: "saldo_pendiente", Value: bson.D{{Key: "$gt", Value: 0}}},
	}, "$cliente_id", "$saldo_pendiente")
	if err != nil {
		return err
	}
	for _, g := range grupos {
		for _, c := range g.clientes {
			id := texto(c["id"])
			c["num_ordenes"] = int64(ordenes[id])
			c["num_ventas"] = int64(ventas[id])
			c["saldo_credito"] = math.Round(saldos[id]*100) / 100
		}
	}
	return nil
}
