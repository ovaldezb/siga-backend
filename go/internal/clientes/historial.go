package clientes

import (
	"context"
	"math"
	"sort"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// Historial atiende GET /reportes/cliente/{id}: el historial 360° del cliente
// (port de get_customer_history_handler en src/handlers/admin/reportes_manager.py).
// Trae citas, OS y ventas de TODAS las sucursales, cada una con `sucursalNombre`,
// y en `metricas.por_sucursal` dónde se ha atendido: el cliente es del taller y
// quien lo recibe en otra sucursal debe ver lo que se le hizo en la primera.
func Historial(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No autorizado", nil), nil
	}
	if platform.EsMecanico(claims) {
		return platform.JSON(req, 403, "Tu usuario no tiene acceso a la informacion de cobros.", nil), nil
	}
	clienteID := req.PathParameters["id"]
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	citas, err := buscarDocs(ctx, db.Collection("citas"), bson.D{{Key: "$or", Value: bson.A{
		bson.D{{Key: "clienteId", Value: clienteID}},
		bson.D{{Key: "cliente_id", Value: clienteID}},
	}}}, "fecha")
	if err != nil {
		return platform.Response{}, err
	}
	// Las OS guardan al cliente en cliente_snapshot.id; algunas viejas en cliente_id.
	ordenes, err := buscarDocs(ctx, db.Collection("ordenes_servicio"), bson.D{{Key: "$or", Value: bson.A{
		bson.D{{Key: "cliente_snapshot.id", Value: clienteID}},
		bson.D{{Key: "cliente_id", Value: clienteID}},
	}}}, "createdAt")
	if err != nil {
		return platform.Response{}, err
	}
	ventas, err := buscarDocs(ctx, db.Collection("ventas"), bson.D{{Key: "cliente_id", Value: clienteID}}, "createdAt")
	if err != nil {
		return platform.Response{}, err
	}

	totalGastado := 0.0
	cantidades := map[string]float64{}
	nombres := []string{} // orden de primera aparición, como el dict de Python
	for _, v := range ventas {
		totalGastado += platform.Numero(v["total"])
		items, _ := v["items"].([]any)
		for _, it := range items {
			item, ok := it.(map[string]any)
			if !ok {
				continue
			}
			prod, _ := item["producto"].(map[string]any)
			nombre := texto(prod["nombre"])
			if nombre == "" {
				nombre = texto(item["nombre"])
			}
			if nombre == "" {
				continue
			}
			cantidad := 1.0
			if c, ok := item["cantidad"]; ok {
				cantidad = platform.Numero(c)
			}
			if _, visto := cantidades[nombre]; !visto {
				nombres = append(nombres, nombre)
			}
			cantidades[nombre] += cantidad
		}
	}
	resumen := make([]map[string]any, 0, len(nombres))
	for _, n := range nombres {
		resumen = append(resumen, map[string]any{"nombre": n, "cantidad": cantidades[n]})
	}
	sort.SliceStable(resumen, func(i, j int) bool {
		return resumen[i]["cantidad"].(float64) > resumen[j]["cantidad"].(float64)
	})

	sucursales, err := nombresSucursales(ctx, db)
	if err != nil {
		return platform.Response{}, err
	}
	conNombreSucursal(citas, sucursales)
	conNombreSucursal(ordenes, sucursales)
	conNombreSucursal(ventas, sucursales)

	return platform.JSON(req, 200, "Historial del cliente recuperado", map[string]any{
		"citas":   citas,
		"ordenes": ordenes,
		"ventas":  ventas,
		"metricas": map[string]any{
			"total_gastado":   math.Round(totalGastado*100) / 100,
			"total_visitas":   len(ventas),
			"resumen_compras": resumen,
			"por_sucursal":    porSucursal(ordenes, ventas, sucursales),
		},
	}), nil
}

func buscarDocs(ctx context.Context, col *mongo.Collection, filtro bson.D, orden string) ([]map[string]any, error) {
	cur, err := col.Find(ctx, filtro, options.Find().SetSort(bson.D{{Key: orden, Value: -1}}))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		out = append(out, platform.Doc(d))
	}
	return out, nil
}

// porSucursal cuenta OS y ventas por sucursal, la de más movimiento primero.
// Los documentos viejos sin sucursal no se cuentan.
func porSucursal(ordenes, ventas []map[string]any, nombres map[string]string) []map[string]any {
	type conteo struct{ ordenes, ventas int }
	conteos := map[string]*conteo{}
	ids := []string{}
	sumar := func(docs []map[string]any, os bool) {
		for _, d := range docs {
			s := sucursalDe(d)
			if s == "" {
				continue
			}
			c := conteos[s]
			if c == nil {
				c = &conteo{}
				conteos[s] = c
				ids = append(ids, s)
			}
			if os {
				c.ordenes++
			} else {
				c.ventas++
			}
		}
	}
	sumar(ordenes, true)
	sumar(ventas, false)
	sort.SliceStable(ids, func(i, j int) bool {
		a, b := conteos[ids[i]], conteos[ids[j]]
		return a.ordenes+a.ventas > b.ordenes+b.ventas
	})
	out := make([]map[string]any, 0, len(ids))
	for _, id := range ids {
		out = append(out, map[string]any{
			"sucursalId": id,
			"nombre":     nombres[id],
			"ordenes":    conteos[id].ordenes,
			"ventas":     conteos[id].ventas,
		})
	}
	return out
}
