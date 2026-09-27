package vehiculos

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
)

// etapasMantenimiento es el mismo pipeline de list_vehiculos_handler, escrito en
// el orden de Python para compararlo línea a línea: dueño del vehículo, última
// OS, próximos cambios (del vehículo o, si faltan, de la última OS), km para el
// aceite, días desde la última visita y el estado del plan de mantenimiento.
// Los %d son dias_vencido, km_umbral y dias_pronto.
//
// Datos legacy que antes tronaban toda la consulta con 500 (y con el filtro de
// mantenimiento el pipeline corre sobre toda la flota, así que bastaba uno): un
// cliente_id que no es ObjectId, un kilometraje o próximo cambio guardado como
// texto, una fecha de próximo cambio que no es texto y una OS con createdAt
// guardado como texto. Ahora se convierten y, si no se puede, cuentan como sin dato.
const etapasMantenimiento = `{"etapas": [
  {"$lookup": {
    "from": "clientes",
    "let": {"cid": "$cliente_id"},
    "pipeline": [
      {"$match": {"$expr": {"$or": [
        {"$eq": ["$_id", "$$cid"]},
        {"$eq": ["$_id", {"$convert": {"input": "$$cid", "to": "objectId", "onError": null, "onNull": null}}]}
      ]}}},
      {"$project": {"nombre": 1, "apellido_paterno": 1, "apellido_materno": 1, "telefono": 1}}
    ],
    "as": "cliente_info"
  }},
  {"$lookup": {
    "from": "ordenes_servicio",
    "let": {"vid": {"$toString": "$_id"}},
    "pipeline": [
      {"$match": {"$expr": {"$eq": ["$vehiculo_id", "$$vid"]}}},
      {"$sort": {"createdAt": -1}},
      {"$limit": 1},
      {"$project": {
        "_id": 0, "createdAt": 1,
        "proximo_cambio_aceite": 1, "proximo_cambio_bujias": 1,
        "proximo_cambio_aceite_anterior": 1, "proximo_cambio_bujias_anterior": 1,
        "proximo_cambio_aceite_fecha": 1, "proximo_cambio_bujias_fecha": 1,
        "proximo_cambio_aceite_fecha_anterior": 1, "proximo_cambio_bujias_fecha_anterior": 1
      }}
    ],
    "as": "ultima_os"
  }},
  {"$addFields": {
    "cliente_nombre": {"$cond": {
      "if": {"$gt": [{"$size": "$cliente_info"}, 0]},
      "then": {"$concat": [
        {"$ifNull": [{"$arrayElemAt": ["$cliente_info.nombre", 0]}, ""]}, " ",
        {"$ifNull": [{"$arrayElemAt": ["$cliente_info.apellido_paterno", 0]}, ""]}, " ",
        {"$ifNull": [{"$arrayElemAt": ["$cliente_info.apellido_materno", 0]}, ""]}
      ]},
      "else": "Cliente Desconocido"
    }},
    "cliente_telefono": {"$arrayElemAt": ["$cliente_info.telefono", 0]},
    "ultima_visita_at": {"$arrayElemAt": ["$ultima_os.createdAt", 0]},
    "proximo_cambio_aceite": {"$cond": {
      "if": {"$gt": [{"$ifNull": ["$proximo_cambio_aceite", 0]}, 0]},
      "then": "$proximo_cambio_aceite",
      "else": {"$arrayElemAt": ["$ultima_os.proximo_cambio_aceite", 0]}
    }},
    "proximo_cambio_bujias": {"$cond": {
      "if": {"$gt": [{"$ifNull": ["$proximo_cambio_bujias", 0]}, 0]},
      "then": "$proximo_cambio_bujias",
      "else": {"$arrayElemAt": ["$ultima_os.proximo_cambio_bujias", 0]}
    }},
    "proximo_cambio_aceite_fecha": {"$cond": {
      "if": {"$and": [{"$eq": [{"$type": "$proximo_cambio_aceite_fecha"}, "string"]}, {"$gt": [{"$strLenCP": "$proximo_cambio_aceite_fecha"}, 0]}]},
      "then": "$proximo_cambio_aceite_fecha",
      "else": {"$arrayElemAt": ["$ultima_os.proximo_cambio_aceite_fecha", 0]}
    }},
    "proximo_cambio_bujias_fecha": {"$cond": {
      "if": {"$and": [{"$eq": [{"$type": "$proximo_cambio_bujias_fecha"}, "string"]}, {"$gt": [{"$strLenCP": "$proximo_cambio_bujias_fecha"}, 0]}]},
      "then": "$proximo_cambio_bujias_fecha",
      "else": {"$arrayElemAt": ["$ultima_os.proximo_cambio_bujias_fecha", 0]}
    }}
  }},
  {"$addFields": {
    "km_para_aceite": {"$let": {
      "vars": {
        "p": {"$cond": [{"$isNumber": "$proximo_cambio_aceite"}, "$proximo_cambio_aceite",
          {"$convert": {"input": "$proximo_cambio_aceite", "to": "double", "onError": null, "onNull": null}}]},
        "k": {"$cond": [{"$isNumber": "$kilometraje"}, "$kilometraje",
          {"$convert": {"input": "$kilometraje", "to": "double", "onError": null, "onNull": null}}]}
      },
      "in": {"$cond": {
        "if": {"$and": [{"$gt": [{"$ifNull": ["$$p", 0]}, 0]}, {"$gt": [{"$ifNull": ["$$k", 0]}, 0]}]},
        "then": {"$subtract": ["$$p", "$$k"]},
        "else": null
      }}
    }},
    "dias_desde_ultima_visita": {"$let": {
      "vars": {"u": {"$convert": {"input": "$ultima_visita_at", "to": "date", "onError": null, "onNull": null}}},
      "in": {"$cond": {
        "if": {"$ifNull": ["$$u", false]},
        "then": {"$dateDiff": {"startDate": "$$u", "endDate": "$$NOW", "unit": "day"}},
        "else": null
      }}
    }}
  }},
  {"$addFields": {
    "mantenimiento_status": {"$switch": {
      "branches": [
        {"case": {"$or": [
          {"$and": [{"$ne": ["$km_para_aceite", null]}, {"$lte": ["$km_para_aceite", 0]}]},
          {"$and": [{"$ne": ["$dias_desde_ultima_visita", null]}, {"$gte": ["$dias_desde_ultima_visita", %d]}]}
        ]}, "then": "vencido"},
        {"case": {"$or": [
          {"$and": [{"$ne": ["$km_para_aceite", null]}, {"$lte": ["$km_para_aceite", %d]}]},
          {"$and": [{"$ne": ["$dias_desde_ultima_visita", null]}, {"$gte": ["$dias_desde_ultima_visita", %d]}]}
        ]}, "then": "pronto"}
      ],
      "default": null
    }}
  }},
  {"$project": {"cliente_info": 0, "ultima_os": 0, "ultima_visita_at": 0}}
]}`

func mantenimiento(diasVencido, kmUmbral, diasPronto int64) ([]bson.D, error) {
	var p struct {
		Etapas []bson.D `bson:"etapas"`
	}
	js := fmt.Sprintf(etapasMantenimiento, diasVencido, kmUmbral, diasPronto)
	if err := bson.UnmarshalExtJSON([]byte(js), false, &p); err != nil {
		return nil, err
	}
	return p.Etapas, nil
}

// enteroO replica int(valor) con respaldo: ausente o no numérico da el default.
func enteroO(qp map[string]string, clave string, defecto int64) int64 {
	raw, ok := qp[clave]
	if !ok {
		return defecto
	}
	n, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	if err != nil {
		return defecto
	}
	return n
}

// List atiende GET /vehiculos (port de list_vehiculos_handler). Filtros:
// sucursalId, cliente_id, search (marca, modelo, placas) y mantenimiento
// (pronto|vencido, con umbrales km_umbral, dias_pronto y dias_vencido). Cada
// vehículo trae su dueño y el estado de su plan de mantenimiento.
// No llama ensure_indexes: los siguen asegurando los handlers Python del tenant.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	qp := req.QueryStringParameters
	clienteID := strings.TrimSpace(qp["cliente_id"])
	search := strings.TrimSpace(qp["search"])
	filtroMant := strings.ToLower(strings.TrimSpace(qp["mantenimiento"]))
	if filtroMant != "pronto" && filtroMant != "vencido" {
		filtroMant = ""
	}
	etapas, err := mantenimiento(enteroO(qp, "dias_vencido", 365), enteroO(qp, "km_umbral", 500), enteroO(qp, "dias_pronto", 180))
	if err != nil {
		return platform.Response{}, err
	}
	page, limit, skip, err := platform.Paginacion(qp, 25)
	if err != nil {
		return platform.Response{}, err
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	filtro := bson.D{}
	if s := qp["sucursalId"]; s != "" {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: s})
	}
	if clienteID != "" {
		filtro = append(filtro, bson.E{Key: "cliente_id", Value: clienteID})
	}
	if search != "" {
		// Python pasaba el texto crudo como regex: un "(" daba 500. Aquí es literal.
		re := bson.Regex{Pattern: regexp.QuoteMeta(search), Options: "i"}
		filtro = append(filtro, bson.E{Key: "$or", Value: bson.A{
			bson.D{{Key: "marca", Value: re}},
			bson.D{{Key: "modelo", Value: re}},
			bson.D{{Key: "placas", Value: re}},
		}})
	}

	col := db.Collection("vehiculos")
	match := bson.D{{Key: "$match", Value: filtro}}
	pagina := []bson.D{
		{{Key: "$sort", Value: bson.D{{Key: "createdAt", Value: -1}}}},
		{{Key: "$skip", Value: skip}},
		{{Key: "$limit", Value: limit}},
	}

	var total int64
	var pipeline mongo.Pipeline
	if filtroMant != "" {
		// El filtro depende del campo calculado: las etapas corren sobre toda la
		// flota y el total sale del mismo pipeline.
		porEstado := bson.D{{Key: "$match", Value: bson.D{{Key: "mantenimiento_status", Value: filtroMant}}}}
		conteo := append(append(mongo.Pipeline{match}, etapas...), porEstado, bson.D{{Key: "$count", Value: "total"}})
		if total, err = contarPipeline(ctx, col, conteo); err != nil {
			return platform.Response{}, err
		}
		pipeline = append(append(append(mongo.Pipeline{match}, etapas...), porEstado), pagina...)
	} else {
		if total, err = col.CountDocuments(ctx, filtro); err != nil {
			return platform.Response{}, err
		}
		// Sin filtro de mantenimiento se recorta la página antes de los $lookup.
		pipeline = append(append(mongo.Pipeline{match}, pagina...), etapas...)
	}

	cur, err := col.Aggregate(ctx, pipeline)
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	vehiculos := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		vehiculos = append(vehiculos, serializar(d))
	}

	totalPages := int64(0)
	if limit > 0 {
		totalPages = (total + limit - 1) / limit
	}
	return platform.JSON(req, 200, "Vehículos obtenidos", map[string]any{
		"items":      vehiculos,
		"total":      total,
		"page":       page,
		"limit":      limit,
		"totalPages": totalPages,
	}), nil
}

func contarPipeline(ctx context.Context, col *mongo.Collection, p mongo.Pipeline) (int64, error) {
	cur, err := col.Aggregate(ctx, p)
	if err != nil {
		return 0, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return 0, err
	}
	if len(filas) == 0 {
		return 0, nil
	}
	return int64(platform.Numero(filas[0]["total"])), nil
}
