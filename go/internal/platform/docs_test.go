package platform

import (
	"reflect"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
)

func TestIsoUTCComoPython(t *testing.T) {
	loc := time.FixedZone("CST", -6*3600)
	if got := IsoUTC(time.Date(2026, 9, 26, 6, 4, 5, 0, loc)); got != "2026-09-26T12:04:05Z" {
		t.Fatalf("sin fracción: %s", got)
	}
	if got := IsoUTC(time.Date(2026, 9, 26, 12, 4, 5, 123_000_000, time.UTC)); got != "2026-09-26T12:04:05.123000Z" {
		t.Fatalf("con milisegundos: %s", got)
	}
}

func TestDocAplana(t *testing.T) {
	oid := bson.NewObjectID()
	fecha := bson.NewDateTimeFromTime(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC))
	got := Doc(bson.M{
		"_id":   oid,
		"fecha": fecha,
		"sub":   bson.D{{Key: "ref", Value: oid}},
		"lista": bson.A{fecha, "x"},
	})
	want := map[string]any{
		"id":    oid.Hex(),
		"fecha": "2026-01-02T03:04:05Z",
		"sub":   map[string]any{"ref": oid.Hex()},
		"lista": []any{"2026-01-02T03:04:05Z", "x"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestGroupsEIsAdmin(t *testing.T) {
	casos := []struct {
		claim any
		admin bool
	}{
		{"[ADMIN ASESOR]", true},
		{"CAJERO,SUPER_ADMIN", true},
		{[]any{"CAJERO"}, false},
		{"[NO_ADMIN]", false}, // is_admin de Python lo aceptaba por substring
		{nil, false},
	}
	for _, c := range casos {
		if got := IsAdmin(map[string]any{"cognito:groups": c.claim}); got != c.admin {
			t.Fatalf("%v: IsAdmin=%v", c.claim, got)
		}
	}
}

func TestParseObjectIDEsClientError(t *testing.T) {
	if _, err := ParseObjectID("xyz", "id"); err == nil {
		t.Fatal("debía fallar")
	} else if _, ok := err.(*ClientError); !ok {
		t.Fatalf("debía ser ClientError: %T", err)
	}
}

func TestTruncarPorCaracteres(t *testing.T) {
	if got := Truncar("ñandú", 3); got != "ñan" {
		t.Fatalf("got %q", got)
	}
}

func TestNumero(t *testing.T) {
	d, _ := bson.ParseDecimal128("1234.565")
	for v, want := range map[any]float64{int32(3): 3, int64(4): 4, 2.5: 2.5, d: 1234.565, "x": 0} {
		if got := Numero(v); got != want {
			t.Fatalf("%v: %v", v, got)
		}
	}
}
