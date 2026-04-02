import os
import json
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from decimal import Decimal

from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor

# ══════════════════════════════════════════════
# App config
# ══════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nexlify-dev-key-change-me")

# ══════════════════════════════════════════════
# External services
# ══════════════════════════════════════════════
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "whatsapp:+14155238886")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ══════════════════════════════════════════════
# Database
# ══════════════════════════════════════════════
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    return conn


def init_db():
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        conn.autocommit = True
        c = conn.cursor()

        c.execute("""CREATE TABLE IF NOT EXISTS negocios (
            id SERIAL PRIMARY KEY, nombre TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
            telefono_whatsapp TEXT, zona_horaria TEXT DEFAULT 'America/Chicago',
            direccion TEXT, telefono_contacto TEXT, activo BOOLEAN DEFAULT TRUE,
            fecha_creacion TIMESTAMPTZ DEFAULT NOW())""")

        c.execute("""CREATE TABLE IF NOT EXISTS servicios (
            id SERIAL PRIMARY KEY, negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            nombre TEXT NOT NULL, duracion_min INTEGER NOT NULL DEFAULT 30,
            precio DECIMAL(10,2) NOT NULL, activo BOOLEAN DEFAULT TRUE)""")

        c.execute("""CREATE TABLE IF NOT EXISTS horarios (
            id SERIAL PRIMARY KEY, negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            dia_semana INTEGER NOT NULL, hora_inicio TIME NOT NULL, hora_fin TIME NOT NULL,
            activo BOOLEAN DEFAULT TRUE, UNIQUE(negocio_id, dia_semana))""")

        c.execute("""CREATE TABLE IF NOT EXISTS citas (
            id SERIAL PRIMARY KEY, negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            servicio_id INTEGER REFERENCES servicios(id), nombre_cliente TEXT NOT NULL,
            telefono_cliente TEXT NOT NULL, fecha DATE NOT NULL, hora_inicio TIME NOT NULL,
            hora_fin TIME NOT NULL, estado TEXT DEFAULT 'confirmada',
            recordatorio_enviado BOOLEAN DEFAULT FALSE, notas TEXT,
            fecha_creacion TIMESTAMPTZ DEFAULT NOW())""")

        c.execute("""CREATE TABLE IF NOT EXISTS conversaciones (
            id SERIAL PRIMARY KEY, negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            telefono_cliente TEXT NOT NULL, mensajes JSONB DEFAULT '[]',
            ultima_actividad TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(negocio_id, telefono_cliente))""")

        conn.close()
        print("Database initialized OK")
    except Exception as e:
        print(f"init_db: {e} (tables may already exist, continuing)")


# ══════════════════════════════════════════════
# Seed: barbería demo
# ══════════════════════════════════════════════
def seed_demo():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM negocios")
        if c.fetchone()["cnt"] == 0:
            c.execute("""INSERT INTO negocios (nombre, slug, zona_horaria, direccion, telefono_contacto)
                VALUES ('Barber Shop Demo', 'barbershop-demo', 'America/Chicago', '1234 Main St, Laredo TX', '(956) 555-0100')
                RETURNING id""")
            nid = c.fetchone()["id"]
            for nombre, dur, precio in [("Corte de cabello",30,15),("Corte + Barba",45,25),("Barba (recorte)",20,10),("Corte de niño",20,12),("Afeitado clásico",30,18),("Diseño de cejas",10,5)]:
                c.execute("INSERT INTO servicios (negocio_id, nombre, duracion_min, precio) VALUES (%s,%s,%s,%s)", (nid,nombre,dur,precio))
            for dia,ini,fin in [(0,"09:00","19:00"),(1,"09:00","19:00"),(2,"09:00","19:00"),(3,"09:00","19:00"),(4,"09:00","19:00"),(5,"09:00","17:00")]:
                c.execute("INSERT INTO horarios (negocio_id, dia_semana, hora_inicio, hora_fin) VALUES (%s,%s,%s,%s)", (nid,dia,ini,fin))
            conn.commit()
            print(f"Demo barbershop created ID {nid}")
        conn.close()
    except Exception as e:
        print(f"seed_demo: {e} (continuing)")


# ══════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════
def obtener_negocio_por_telefono(telefono_cliente):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM negocios WHERE activo = TRUE ORDER BY id LIMIT 1")
    negocio = c.fetchone()
    conn.close()
    return negocio

def obtener_servicios(negocio_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM servicios WHERE negocio_id = %s AND activo = TRUE ORDER BY precio", (negocio_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def obtener_horario_hoy(negocio_id, zona_horaria="America/Chicago"):
    tz = ZoneInfo(zona_horaria)
    dia = datetime.now(tz).weekday()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM horarios WHERE negocio_id = %s AND dia_semana = %s AND activo = TRUE", (negocio_id, dia))
    h = c.fetchone()
    conn.close()
    return h

def obtener_horarios_semana(negocio_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM horarios WHERE negocio_id = %s AND activo = TRUE ORDER BY dia_semana", (negocio_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def obtener_citas_dia(negocio_id, fecha):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT c.*, s.nombre as servicio_nombre, s.precio as servicio_precio, s.duracion_min
        FROM citas c JOIN servicios s ON c.servicio_id = s.id
        WHERE c.negocio_id = %s AND c.fecha = %s ORDER BY c.hora_inicio""", (negocio_id, fecha))
    rows = c.fetchall()
    conn.close()
    return rows

def obtener_disponibilidad(negocio_id, fecha, duracion_min=30, zona_horaria="America/Chicago"):
    dia_semana = fecha.weekday()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM horarios WHERE negocio_id = %s AND dia_semana = %s AND activo = TRUE", (negocio_id, dia_semana))
    horario = c.fetchone()
    if not horario:
        conn.close()
        return []
    c.execute("SELECT hora_inicio, hora_fin FROM citas WHERE negocio_id = %s AND fecha = %s AND estado != 'cancelada' ORDER BY hora_inicio", (negocio_id, fecha))
    citas_ex = c.fetchall()
    conn.close()

    slots = []
    hora_actual = datetime.combine(fecha, horario["hora_inicio"])
    hora_cierre = datetime.combine(fecha, horario["hora_fin"])
    tz = ZoneInfo(zona_horaria)
    ahora = datetime.now(tz)

    while hora_actual + timedelta(minutes=duracion_min) <= hora_cierre:
        si = hora_actual.time()
        sf = (hora_actual + timedelta(minutes=duracion_min)).time()
        conflicto = any(si < cx["hora_fin"] and sf > cx["hora_inicio"] for cx in citas_ex)
        if not conflicto:
            if fecha == ahora.date() and si <= ahora.time():
                hora_actual += timedelta(minutes=30)
                continue
            slots.append(si.strftime("%I:%M %p"))
        hora_actual += timedelta(minutes=30)
    return slots

def guardar_cita(negocio_id, servicio_id, nombre, telefono, fecha, hora_inicio, hora_fin):
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO citas (negocio_id, servicio_id, nombre_cliente, telefono_cliente, fecha, hora_inicio, hora_fin)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (negocio_id, servicio_id, nombre, telefono, fecha, hora_inicio, hora_fin))
    cita_id = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return cita_id

def cancelar_cita(cita_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE citas SET estado = 'cancelada' WHERE id = %s", (cita_id,))
    conn.commit()
    conn.close()

def obtener_cita_activa(negocio_id, telefono):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT c.*, s.nombre as servicio_nombre, s.precio
        FROM citas c JOIN servicios s ON c.servicio_id = s.id
        WHERE c.negocio_id = %s AND c.telefono_cliente = %s
        AND c.estado = 'confirmada' AND c.fecha >= CURRENT_DATE
        ORDER BY c.fecha, c.hora_inicio LIMIT 1""", (negocio_id, telefono))
    cita = c.fetchone()
    conn.close()
    return cita

def obtener_conversacion(negocio_id, telefono):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT mensajes FROM conversaciones WHERE negocio_id = %s AND telefono_cliente = %s", (negocio_id, telefono))
    row = c.fetchone()
    conn.close()
    return row["mensajes"] if row else []

def guardar_conversacion(negocio_id, telefono, mensajes):
    conn = get_db()
    c = conn.cursor()
    mensajes = mensajes[-12:]
    c.execute("""INSERT INTO conversaciones (negocio_id, telefono_cliente, mensajes, ultima_actividad)
        VALUES (%s, %s, %s, NOW()) ON CONFLICT (negocio_id, telefono_cliente)
        DO UPDATE SET mensajes = %s, ultima_actividad = NOW()""",
        (negocio_id, telefono, json.dumps(mensajes), json.dumps(mensajes)))
    conn.commit()
    conn.close()

def fecha_hora_actual(zona_horaria="America/Chicago"):
    tz = ZoneInfo(zona_horaria)
    ahora = datetime.now(tz)
    dias = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]
    return f"{dias[ahora.weekday()]} {ahora.strftime('%d/%m/%Y')} - {ahora.strftime('%I:%M %p')}"

def proximos_dias_disponibles(negocio_id, zona_horaria="America/Chicago", num_dias=5):
    tz = ZoneInfo(zona_horaria)
    hoy = datetime.now(tz).date()
    resumen = []
    nombres = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"]
    for i in range(num_dias):
        fecha = hoy + timedelta(days=i)
        slots = obtener_disponibilidad(negocio_id, fecha, zona_horaria=zona_horaria)
        dia = "Hoy" if i == 0 else ("Mañana" if i == 1 else nombres[fecha.weekday()])
        if slots:
            muestra = ", ".join(slots[:5])
            extra = f" (+{len(slots)-5} más)" if len(slots) > 5 else ""
            resumen.append(f"  {dia} {fecha.strftime('%d/%m')}: {muestra}{extra}")
        else:
            resumen.append(f"  {dia} {fecha.strftime('%d/%m')}: Cerrado/Lleno")
    return "\n".join(resumen)


# ══════════════════════════════════════════════
# Claude AI — System prompt
# ══════════════════════════════════════════════
def build_system_prompt(negocio, servicios, cita_activa, disponibilidad_resumen):
    tz = negocio["zona_horaria"]
    fecha = fecha_hora_actual(tz)
    srv_list = "\n".join([f"  • {s['nombre']} — ${s['precio']:.0f} ({s['duracion_min']}min)" for s in servicios])

    horarios = obtener_horarios_semana(negocio["id"])
    dias_nombres = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]
    dias_con_horario = set()
    horario_lines = []
    for h in horarios:
        dias_con_horario.add(h["dia_semana"])
        horario_lines.append(f"  {dias_nombres[h['dia_semana']]}: {h['hora_inicio'].strftime('%I:%M %p')} - {h['hora_fin'].strftime('%I:%M %p')}")
    for i in range(7):
        if i not in dias_con_horario:
            horario_lines.append(f"  {dias_nombres[i]}: CERRADO")
    horario_str = "\n".join(horario_lines)

    prompt = f"""Eres el asistente virtual de {negocio['nombre']} por WhatsApp. Amable, profesional y directo.
Responde en el MISMO idioma del cliente.

HOY ES: {fecha}
IMPORTANTE: La fecha de HOY es {datetime.now(ZoneInfo(tz)).strftime('%d/%m/%Y')}. Cuando digas "hoy" o "mañana", verifica que corresponda a esta fecha. NO digas "mañana" si la fecha de la cita es la misma que HOY.
DIRECCIÓN: {negocio['direccion'] or 'No configurada'}
TEL: {negocio['telefono_contacto'] or 'No configurado'}

SERVICIOS:
{srv_list}

HORARIOS:
{horario_str}

DISPONIBILIDAD PRÓXIMOS DÍAS:
{disponibilidad_resumen}
"""

    if cita_activa:
        prompt += f"""
CITA ACTIVA DEL CLIENTE:
  #{cita_activa['id']}: {cita_activa['servicio_nombre']} — {cita_activa['fecha'].strftime('%d/%m/%Y')} a las {cita_activa['hora_inicio'].strftime('%I:%M %p')} — ${cita_activa['precio']:.0f}
"""

    prompt += """
PARA AGENDAR — cuando tengas nombre, servicio, fecha y hora, responde EXACTAMENTE:

CITA CONFIRMADA
Nombre: [nombre]
Servicio: [nombre EXACTO del servicio]
Fecha: [DD/MM/YYYY]
Hora: [HH:MM AM/PM]

PARA CANCELAR — si tiene cita activa y quiere cancelar:

CANCELACION CONFIRMADA
Cita: #[id]

PARA REAGENDAR — cancela la actual y pregunta nueva fecha:

REAGENDAR
Cita cancelada: #[id]

REGLAS:
- Máximo 3-4 líneas. Sé breve y claro.
- NO inventes servicios ni precios.
- Si piden horario no disponible, sugiere los más cercanos.
- Calcula fechas: "hoy", "mañana", "el viernes" = fecha real según FECHA ACTUAL.
- NO pongas número de cita en la confirmación, el sistema lo agrega.
- Sin cita activa y quieren cancelar = diles que no tienen citas pendientes.
- Cerrado hoy = sugiere próximo día disponible.

PERSONALIDAD:
- Usa emojis con moderación para que el chat se sienta ameno: ✂️ 💈 📅 ⏰ ✅ 👋 😊 🙌 👍
- Sé cálido y amigable como un barbero que conoce a sus clientes.
- Usa expresiones naturales: "¡Listo!", "¡Perfecto!", "¡Te esperamos!", "¡Con gusto!"
- Si el cliente es informal, sé informal. Si es formal, sé formal.
- Haz que el cliente sienta que habla con alguien real, no con un robot."""

    return prompt


# ══════════════════════════════════════════════
# Parsers
# ══════════════════════════════════════════════
def parsear_confirmacion(texto):
    data = {}
    for linea in texto.split("\n"):
        l = linea.replace("*","").strip()
        if "Nombre:" in l: data["nombre"] = l.split("Nombre:")[1].strip()
        elif "Servicio:" in l: data["servicio"] = l.split("Servicio:")[1].strip()
        elif "Fecha:" in l: data["fecha"] = l.split("Fecha:")[1].strip()
        elif "Hora:" in l: data["hora"] = l.split("Hora:")[1].strip()
    return data

def parsear_cancelacion(texto):
    m = re.search(r"Cita.*?#(\d+)", texto)
    return int(m.group(1)) if m else None

def parsear_reagendar(texto):
    m = re.search(r"Cita cancelada.*?#(\d+)", texto)
    return int(m.group(1)) if m else None

def encontrar_servicio(servicios, nombre_buscado):
    nl = nombre_buscado.lower().strip()
    for s in servicios:
        if s["nombre"].lower() == nl:
            return s
    for s in servicios:
        if nl in s["nombre"].lower() or s["nombre"].lower() in nl:
            return s
    return None

def parsear_fecha(texto_fecha, zona_horaria="America/Chicago"):
    tz = ZoneInfo(zona_horaria)
    hoy = datetime.now(tz).date()
    for fmt in ["%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y"]:
        try:
            return datetime.strptime(texto_fecha.strip(), fmt).date()
        except ValueError:
            pass
    return hoy

def parsear_hora(texto_hora):
    texto = texto_hora.strip().upper()
    for fmt in ["%I:%M %p", "%I:%M%p", "%H:%M", "%I %p", "%I%p"]:
        try:
            return datetime.strptime(texto, fmt).time()
        except ValueError:
            pass
    return None


# ══════════════════════════════════════════════
# WhatsApp Webhook
# ══════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    telefono = request.form.get("From", "")
    mensaje = request.form.get("Body", "").strip()
    if not telefono or not mensaje:
        return str(MessagingResponse())

    negocio = obtener_negocio_por_telefono(telefono)
    if not negocio:
        resp = MessagingResponse()
        resp.message("Lo sentimos, no hay un negocio configurado en este momento.")
        return str(resp)

    neg_id = negocio["id"]
    tz = negocio["zona_horaria"]
    servicios = obtener_servicios(neg_id)
    cita_activa = obtener_cita_activa(neg_id, telefono)
    disponibilidad_resumen = proximos_dias_disponibles(neg_id, tz)

    historial = obtener_conversacion(neg_id, telefono)
    historial.append({"role": "user", "content": mensaje})

    system_prompt = build_system_prompt(negocio, servicios, cita_activa, disponibilidad_resumen)

    try:
        respuesta = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=500,
            system=system_prompt,
            messages=historial
        )
        texto_respuesta = respuesta.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}")
        texto_respuesta = "Disculpa, tuve un problema técnico. ¿Podrías intentar de nuevo?"

    historial.append({"role": "assistant", "content": texto_respuesta})
    guardar_conversacion(neg_id, telefono, historial)

    ahora = datetime.now(ZoneInfo(tz))

    # ── REAGENDAR ──
    if "REAGENDAR" in texto_respuesta:
        cita_id = parsear_reagendar(texto_respuesta)
        if cita_id:
            cancelar_cita(cita_id)
            texto_respuesta = re.sub(r"REAGENDAR\s*\n.*?#\d+\s*\n?", "", texto_respuesta).strip()
            if not texto_respuesta:
                texto_respuesta = f"Tu cita #{cita_id} fue cancelada. ¿Para cuándo quieres la nueva cita?"

    # ── CITA CONFIRMADA (supports multiple in one message) ──
    elif "CITA CONFIRMADA" in texto_respuesta:
        # Split response by CITA CONFIRMADA blocks
        bloques = texto_respuesta.split("CITA CONFIRMADA")
        citas_creadas = []

        for bloque in bloques[1:]:  # Skip text before first CITA CONFIRMADA
            datos = parsear_confirmacion("CITA CONFIRMADA" + bloque)
            if datos.get("nombre") and datos.get("servicio") and datos.get("hora"):
                servicio = encontrar_servicio(servicios, datos["servicio"])
                if servicio:
                    fecha = parsear_fecha(datos.get("fecha", ""), tz) if datos.get("fecha") else ahora.date()
                    hora_inicio = parsear_hora(datos["hora"])
                    if hora_inicio:
                        hora_fin = (datetime.combine(fecha, hora_inicio) + timedelta(minutes=servicio["duracion_min"])).time()

                        # Anti-duplicate check
                        conn = get_db()
                        cur = conn.cursor()
                        cur.execute("""SELECT id FROM citas WHERE negocio_id = %s AND telefono_cliente = %s
                            AND fecha = %s AND hora_inicio = %s AND estado = 'confirmada'
                            AND nombre_cliente = %s""",
                            (neg_id, telefono, fecha, hora_inicio, datos["nombre"]))
                        duplicada = cur.fetchone()
                        conn.close()

                        if not duplicada:
                            cita_id = guardar_cita(neg_id, servicio["id"], datos["nombre"], telefono, fecha, hora_inicio, hora_fin)
                            citas_creadas.append(f"#{cita_id} ({datos['nombre']})")

        if citas_creadas:
            if len(citas_creadas) == 1:
                texto_respuesta += f"\n\nTu cita es la {citas_creadas[0]}."
            else:
                texto_respuesta += f"\n\nCitas registradas: {', '.join(citas_creadas)}."

    # ── CANCELACION ──
    elif "CANCELACION CONFIRMADA" in texto_respuesta:
        cita_id = parsear_cancelacion(texto_respuesta)
        if cita_id:
            cancelar_cita(cita_id)
            texto_respuesta = f"Tu cita #{cita_id} ha sido cancelada. Si cambias de opinión, escríbenos para reagendar. ¡Hasta pronto!"
        elif cita_activa:
            cancelar_cita(cita_activa["id"])
            texto_respuesta = f"Tu cita #{cita_activa['id']} ha sido cancelada. Si cambias de opinión, escríbenos para reagendar. ¡Hasta pronto!"

    resp = MessagingResponse()
    resp.message(texto_respuesta)
    return str(resp)


# ══════════════════════════════════════════════
# API endpoints
# ══════════════════════════════════════════════
@app.route("/api/citas/<int:negocio_id>")
def api_citas(negocio_id):
    fecha_str = request.args.get("fecha")
    fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else datetime.now().date()
    citas = obtener_citas_dia(negocio_id, fecha)
    return jsonify([{
        "id": c["id"],
        "nombre_cliente": c["nombre_cliente"],
        "telefono_cliente": c["telefono_cliente"],
        "fecha": c["fecha"].isoformat(),
        "hora_inicio": c["hora_inicio"].strftime("%I:%M %p"),
        "hora_fin": c["hora_fin"].strftime("%I:%M %p"),
        "estado": c["estado"],
        "servicio_nombre": c.get("servicio_nombre", "Servicio"),
        "servicio_precio": float(c["servicio_precio"]) if c.get("servicio_precio") else 0,
        "duracion_min": c.get("duracion_min", 30),
        "notas": c["notas"],
    } for c in citas])


@app.route("/api/citas/<int:cita_id>/estado", methods=["POST"])
def api_cambiar_estado(cita_id):
    data = request.json
    nuevo_estado = data.get("estado")
    if nuevo_estado not in ("confirmada", "completada", "cancelada", "no_show"):
        return jsonify({"error": "Estado inválido"}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE citas SET estado = %s WHERE id = %s", (nuevo_estado, cita_id))

    # Notify client
    c.execute("SELECT telefono_cliente, negocio_id FROM citas WHERE id = %s", (cita_id,))
    cita = c.fetchone()
    if cita:
        c.execute("SELECT nombre FROM negocios WHERE id = %s", (cita["negocio_id"],))
        neg = c.fetchone()
        try:
            if nuevo_estado == "cancelada":
                twilio_client.messages.create(
                    body=f"Tu cita #{cita_id} en {neg['nombre']} ha sido cancelada. Disculpa el inconveniente.",
                    from_=TWILIO_NUMBER, to=cita["telefono_cliente"])
            elif nuevo_estado == "completada":
                twilio_client.messages.create(
                    body=f"¡Gracias por visitarnos en {neg['nombre']}! Esperamos verte pronto.",
                    from_=TWILIO_NUMBER, to=cita["telefono_cliente"])
        except Exception as e:
            print(f"Twilio notify error: {e}")

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/servicios/<int:negocio_id>")
def api_servicios(negocio_id):
    servicios = obtener_servicios(negocio_id)
    return jsonify([{"id": s["id"], "nombre": s["nombre"], "duracion_min": s["duracion_min"],
                     "precio": float(s["precio"]), "activo": s["activo"]} for s in servicios])


@app.route("/api/stats/<int:negocio_id>")
def api_stats(negocio_id):
    fecha_str = request.args.get("fecha")
    fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else datetime.now().date()

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as t FROM citas WHERE negocio_id=%s AND fecha=%s AND estado!='cancelada'", (negocio_id, fecha))
    hoy_total = c.fetchone()["t"]
    c.execute("SELECT COUNT(*) as t FROM citas WHERE negocio_id=%s AND fecha=%s AND estado='completada'", (negocio_id, fecha))
    completadas = c.fetchone()["t"]
    c.execute("SELECT COUNT(*) as t FROM citas WHERE negocio_id=%s AND fecha=%s AND estado='cancelada'", (negocio_id, fecha))
    canceladas = c.fetchone()["t"]
    c.execute("SELECT COUNT(*) as t FROM citas WHERE negocio_id=%s AND fecha=%s AND estado='no_show'", (negocio_id, fecha))
    no_shows = c.fetchone()["t"]
    c.execute("""SELECT COALESCE(SUM(s.precio),0) as t FROM citas c JOIN servicios s ON c.servicio_id=s.id
        WHERE c.negocio_id=%s AND c.fecha=%s AND c.estado='completada'""", (negocio_id, fecha))
    ingresos = float(c.fetchone()["t"])
    conn.close()

    return jsonify({"hoy_total": hoy_total, "completadas": completadas,
                     "canceladas": canceladas, "no_shows": no_shows, "ingresos": ingresos})


# ══════════════════════════════════════════════
# Dashboard routes
# ══════════════════════════════════════════════
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/dashboard/<slug>")
def dashboard(slug):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM negocios WHERE slug = %s AND activo = TRUE", (slug,))
    negocio = c.fetchone()
    conn.close()
    if not negocio:
        return "Negocio no encontrado", 404
    return render_template("dashboard.html", negocio=negocio)


# ══════════════════════════════════════════════
# Init & Run
# ══════════════════════════════════════════════
with app.app_context():
    init_db()
    seed_demo()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
