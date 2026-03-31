import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
    """Create all tables if they don't exist."""
    conn = get_db()
    c = conn.cursor()

    # Negocios (tenants)
    c.execute("""
        CREATE TABLE IF NOT EXISTS negocios (
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            telefono_whatsapp TEXT,
            zona_horaria TEXT DEFAULT 'America/Chicago',
            direccion TEXT,
            telefono_contacto TEXT,
            activo BOOLEAN DEFAULT TRUE,
            fecha_creacion TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Servicios del negocio
    c.execute("""
        CREATE TABLE IF NOT EXISTS servicios (
            id SERIAL PRIMARY KEY,
            negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            nombre TEXT NOT NULL,
            duracion_min INTEGER NOT NULL DEFAULT 30,
            precio DECIMAL(10,2) NOT NULL,
            activo BOOLEAN DEFAULT TRUE
        )
    """)

    # Horarios del negocio (por día de semana)
    c.execute("""
        CREATE TABLE IF NOT EXISTS horarios (
            id SERIAL PRIMARY KEY,
            negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            dia_semana INTEGER NOT NULL,
            hora_inicio TIME NOT NULL,
            hora_fin TIME NOT NULL,
            activo BOOLEAN DEFAULT TRUE,
            UNIQUE(negocio_id, dia_semana)
        )
    """)

    # Citas
    c.execute("""
        CREATE TABLE IF NOT EXISTS citas (
            id SERIAL PRIMARY KEY,
            negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            servicio_id INTEGER REFERENCES servicios(id),
            nombre_cliente TEXT NOT NULL,
            telefono_cliente TEXT NOT NULL,
            fecha DATE NOT NULL,
            hora_inicio TIME NOT NULL,
            hora_fin TIME NOT NULL,
            estado TEXT DEFAULT 'confirmada',
            recordatorio_enviado BOOLEAN DEFAULT FALSE,
            notas TEXT,
            fecha_creacion TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Conversaciones activas (para contexto de Claude)
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversaciones (
            id SERIAL PRIMARY KEY,
            negocio_id INTEGER REFERENCES negocios(id) ON DELETE CASCADE,
            telefono_cliente TEXT NOT NULL,
            mensajes JSONB DEFAULT '[]',
            ultima_actividad TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(negocio_id, telefono_cliente)
        )
    """)

    conn.commit()
    conn.close()


# ══════════════════════════════════════════════
# Seed: barbería demo
# ══════════════════════════════════════════════
def seed_demo():
    """Insert demo barbershop if no businesses exist."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM negocios")
    if c.fetchone()["cnt"] == 0:
        # Crear barbería demo
        c.execute("""
            INSERT INTO negocios (nombre, slug, zona_horaria, direccion, telefono_contacto)
            VALUES ('Barber Shop Demo', 'barbershop-demo', 'America/Chicago', '1234 Main St, Laredo TX', '(956) 555-0100')
            RETURNING id
        """)
        negocio_id = c.fetchone()["id"]

        # Servicios
        servicios = [
            ("Corte de cabello", 30, 15.00),
            ("Corte + Barba", 45, 25.00),
            ("Barba (recorte)", 20, 10.00),
            ("Corte de niño", 20, 12.00),
            ("Afeitado clásico", 30, 18.00),
            ("Diseño de cejas", 10, 5.00),
        ]
        for nombre, dur, precio in servicios:
            c.execute(
                "INSERT INTO servicios (negocio_id, nombre, duracion_min, precio) VALUES (%s, %s, %s, %s)",
                (negocio_id, nombre, dur, precio)
            )

        # Horarios: Lunes-Viernes 9am-7pm, Sábado 9am-5pm, Domingo cerrado
        horarios = [
            (0, "09:00", "19:00"),  # Lunes
            (1, "09:00", "19:00"),  # Martes
            (2, "09:00", "19:00"),  # Miércoles
            (3, "09:00", "19:00"),  # Jueves
            (4, "09:00", "19:00"),  # Viernes
            (5, "09:00", "17:00"),  # Sábado
        ]
        for dia, inicio, fin in horarios:
            c.execute(
                "INSERT INTO horarios (negocio_id, dia_semana, hora_inicio, hora_fin) VALUES (%s, %s, %s, %s)",
                (negocio_id, dia, inicio, fin)
            )

        conn.commit()
        print(f"Demo barbershop created with ID {negocio_id}")
    conn.close()


# ══════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════
def obtener_negocio_por_telefono(telefono_cliente):
    """Find which business a WhatsApp number is chatting with.
    For now, return the first active business (single-tenant mode).
    Multi-tenant routing will come in Phase 5.
    """
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
    servicios = c.fetchall()
    conn.close()
    return servicios


def obtener_horario_hoy(negocio_id, zona_horaria="America/Chicago"):
    tz = ZoneInfo(zona_horaria)
    ahora = datetime.now(tz)
    dia = ahora.weekday()
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM horarios WHERE negocio_id = %s AND dia_semana = %s AND activo = TRUE",
        (negocio_id, dia)
    )
    horario = c.fetchone()
    conn.close()
    return horario


def negocio_abierto(negocio_id, zona_horaria="America/Chicago"):
    horario = obtener_horario_hoy(negocio_id, zona_horaria)
    if not horario:
        return False, None
    tz = ZoneInfo(zona_horaria)
    ahora = datetime.now(tz).time()
    abierto = horario["hora_inicio"] <= ahora <= horario["hora_fin"]
    return abierto, horario


def obtener_citas_dia(negocio_id, fecha):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM citas WHERE negocio_id = %s AND fecha = %s AND estado != 'cancelada' ORDER BY hora_inicio",
        (negocio_id, fecha)
    )
    citas = c.fetchall()
    conn.close()
    return citas


def obtener_disponibilidad(negocio_id, fecha, duracion_min=30, zona_horaria="America/Chicago"):
    """Get available time slots for a given date."""
    dia_semana = fecha.weekday()
    conn = get_db()
    c = conn.cursor()

    # Get business hours for that day
    c.execute(
        "SELECT * FROM horarios WHERE negocio_id = %s AND dia_semana = %s AND activo = TRUE",
        (negocio_id, dia_semana)
    )
    horario = c.fetchone()
    if not horario:
        conn.close()
        return []  # Closed that day

    # Get existing appointments
    c.execute(
        "SELECT hora_inicio, hora_fin FROM citas WHERE negocio_id = %s AND fecha = %s AND estado != 'cancelada' ORDER BY hora_inicio",
        (negocio_id, fecha)
    )
    citas_existentes = c.fetchall()
    conn.close()

    # Generate slots every 30 min
    slots = []
    hora_actual = datetime.combine(fecha, horario["hora_inicio"])
    hora_cierre = datetime.combine(fecha, horario["hora_fin"])

    while hora_actual + timedelta(minutes=duracion_min) <= hora_cierre:
        slot_inicio = hora_actual.time()
        slot_fin = (hora_actual + timedelta(minutes=duracion_min)).time()

        # Check if slot conflicts with existing appointments
        conflicto = False
        for cita in citas_existentes:
            if slot_inicio < cita["hora_fin"] and slot_fin > cita["hora_inicio"]:
                conflicto = True
                break

        if not conflicto:
            # Skip past times if it's today
            tz = ZoneInfo(zona_horaria)
            ahora = datetime.now(tz)
            if fecha == ahora.date() and slot_inicio <= ahora.time():
                hora_actual += timedelta(minutes=30)
                continue
            slots.append(slot_inicio.strftime("%I:%M %p"))

        hora_actual += timedelta(minutes=30)

    return slots


def guardar_cita(negocio_id, servicio_id, nombre, telefono, fecha, hora_inicio, hora_fin):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO citas (negocio_id, servicio_id, nombre_cliente, telefono_cliente, fecha, hora_inicio, hora_fin)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (negocio_id, servicio_id, nombre, telefono, fecha, hora_inicio, hora_fin))
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
    c.execute("""
        SELECT c.*, s.nombre as servicio_nombre, s.precio
        FROM citas c
        JOIN servicios s ON c.servicio_id = s.id
        WHERE c.negocio_id = %s AND c.telefono_cliente = %s
        AND c.estado = 'confirmada' AND c.fecha >= CURRENT_DATE
        ORDER BY c.fecha, c.hora_inicio LIMIT 1
    """, (negocio_id, telefono))
    cita = c.fetchone()
    conn.close()
    return cita


def obtener_conversacion(negocio_id, telefono):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT mensajes FROM conversaciones WHERE negocio_id = %s AND telefono_cliente = %s",
        (negocio_id, telefono)
    )
    row = c.fetchone()
    conn.close()
    return row["mensajes"] if row else []


def guardar_conversacion(negocio_id, telefono, mensajes):
    conn = get_db()
    c = conn.cursor()
    # Keep only last 12 messages
    mensajes = mensajes[-12:]
    c.execute("""
        INSERT INTO conversaciones (negocio_id, telefono_cliente, mensajes, ultima_actividad)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (negocio_id, telefono_cliente)
        DO UPDATE SET mensajes = %s, ultima_actividad = NOW()
    """, (negocio_id, telefono, json.dumps(mensajes), json.dumps(mensajes)))
    conn.commit()
    conn.close()


def fecha_hora_actual(zona_horaria="America/Chicago"):
    tz = ZoneInfo(zona_horaria)
    ahora = datetime.now(tz)
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    return f"{dias[ahora.weekday()]} {ahora.strftime('%d/%m/%Y')} - {ahora.strftime('%I:%M %p')}"


# ══════════════════════════════════════════════
# Claude AI — System prompt builder
# ══════════════════════════════════════════════
def build_system_prompt(negocio, servicios, horario_hoy, cita_activa, disponibilidad_hoy):
    tz = negocio["zona_horaria"]
    fecha = fecha_hora_actual(tz)

    # Format services list
    srv_list = " | ".join([f"{s['nombre']} ${s['precio']:.0f} ({s['duracion_min']}min)" for s in servicios])

    # Format today's hours
    if horario_hoy:
        horario_str = f"{horario_hoy['hora_inicio'].strftime('%I:%M %p')} - {horario_hoy['hora_fin'].strftime('%I:%M %p')}"
    else:
        horario_str = "CERRADO HOY"

    # Format availability
    if disponibilidad_hoy:
        slots_str = ", ".join(disponibilidad_hoy[:8])  # Show max 8 slots
        if len(disponibilidad_hoy) > 8:
            slots_str += f" (y {len(disponibilidad_hoy)-8} más)"
    else:
        slots_str = "Sin horarios disponibles hoy"

    prompt = f"""Eres el asistente virtual de {negocio['nombre']}. Habla en el idioma del cliente (español o inglés).

FECHA Y HORA: {fecha}
DIRECCIÓN: {negocio['direccion'] or 'No configurada'}
TEL CONTACTO: {negocio['telefono_contacto'] or 'No configurado'}

SERVICIOS: {srv_list}

HORARIO HOY: {horario_str}
DISPONIBILIDAD HOY: {slots_str}

"""

    if cita_activa:
        prompt += f"""CITA ACTIVA DEL CLIENTE:
- Cita #{cita_activa['id']}: {cita_activa['servicio_nombre']} el {cita_activa['fecha'].strftime('%d/%m/%Y')} a las {cita_activa['hora_inicio'].strftime('%I:%M %p')}
- Precio: ${cita_activa['precio']:.0f}

"""

    prompt += """REGLAS:
- Ayuda al cliente a agendar citas. Pregunta qué servicio quiere, su nombre y la hora.
- Cuando tengas toda la info, confirma usando EXACTAMENTE este formato:

CITA CONFIRMADA
Nombre: [nombre]
Servicio: [nombre servicio exacto del menú]
Fecha: [DD/MM/YYYY]
Hora: [HH:MM AM/PM]

- Si el cliente quiere cancelar y tiene cita activa, responde:

CANCELACION CONFIRMADA
Cita: #[id de la cita activa]

- Si preguntan por disponibilidad, muestra los horarios libres de hoy.
- Si el horario que piden no está disponible, sugiere los más cercanos.
- Máximo 3-4 líneas por respuesta.
- No inventes servicios ni precios que no estén en la lista.
- Si hoy está cerrado, diles cuándo abren (siguiente día hábil).
- Sé amable, profesional y breve."""

    return prompt


# ══════════════════════════════════════════════
# Parse Claude's response for actions
# ══════════════════════════════════════════════
def parsear_confirmacion(texto):
    data = {}
    for linea in texto.split("\n"):
        if "Nombre:" in linea:
            data["nombre"] = linea.split("Nombre:")[1].strip()
        elif "Servicio:" in linea:
            data["servicio"] = linea.split("Servicio:")[1].strip()
        elif "Fecha:" in linea:
            data["fecha"] = linea.split("Fecha:")[1].strip()
        elif "Hora:" in linea:
            data["hora"] = linea.split("Hora:")[1].strip()
    return data


def parsear_cancelacion(texto):
    match = re.search(r"Cita:\s*#(\d+)", texto)
    return int(match.group(1)) if match else None


# ══════════════════════════════════════════════
# WhatsApp Webhook
# ══════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    telefono = request.form.get("From", "")
    mensaje = request.form.get("Body", "").strip()

    if not telefono or not mensaje:
        return str(MessagingResponse())

    # Find the business this customer is chatting with
    negocio = obtener_negocio_por_telefono(telefono)
    if not negocio:
        resp = MessagingResponse()
        resp.message("Lo sentimos, no hay un negocio configurado en este momento.")
        return str(resp)

    neg_id = negocio["id"]
    tz = negocio["zona_horaria"]

    # Load context
    servicios = obtener_servicios(neg_id)
    horario_hoy = obtener_horario_hoy(neg_id, tz)
    cita_activa = obtener_cita_activa(neg_id, telefono)

    ahora = datetime.now(ZoneInfo(tz))
    disponibilidad = obtener_disponibilidad(neg_id, ahora.date(), zona_horaria=tz)

    # Build conversation
    historial = obtener_conversacion(neg_id, telefono)
    historial.append({"role": "user", "content": mensaje})

    # Call Claude
    system_prompt = build_system_prompt(negocio, servicios, horario_hoy, cita_activa, disponibilidad)

    respuesta = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        system=system_prompt,
        messages=historial
    )
    texto_respuesta = respuesta.content[0].text

    # Save conversation
    historial.append({"role": "assistant", "content": texto_respuesta})
    guardar_conversacion(neg_id, telefono, historial)

    # Process actions in Claude's response
    if "CITA CONFIRMADA" in texto_respuesta:
        datos = parsear_confirmacion(texto_respuesta)
        if datos.get("nombre") and datos.get("servicio") and datos.get("hora"):
            # Find the service
            servicio = next((s for s in servicios if s["nombre"].lower() == datos["servicio"].lower()), None)
            if servicio:
                # Parse date (default today if not specified)
                try:
                    fecha = datetime.strptime(datos["fecha"], "%d/%m/%Y").date()
                except (ValueError, KeyError):
                    fecha = ahora.date()

                # Parse time
                try:
                    hora_inicio = datetime.strptime(datos["hora"], "%I:%M %p").time()
                    hora_fin_dt = datetime.combine(fecha, hora_inicio) + timedelta(minutes=servicio["duracion_min"])
                    hora_fin = hora_fin_dt.time()
                except ValueError:
                    hora_inicio = None
                    hora_fin = None

                if hora_inicio and hora_fin:
                    cita_id = guardar_cita(
                        neg_id, servicio["id"], datos["nombre"],
                        telefono, fecha, hora_inicio, hora_fin
                    )
                    texto_respuesta += f"\n\nTu cita es la #{cita_id}. ¡Te esperamos!"

    if "CANCELACION CONFIRMADA" in texto_respuesta:
        cita_id = parsear_cancelacion(texto_respuesta)
        if cita_id:
            cancelar_cita(cita_id)
            texto_respuesta = f"Tu cita #{cita_id} ha sido cancelada. Si cambias de opinión, escríbenos para reagendar."

    # Send response
    resp = MessagingResponse()
    resp.message(texto_respuesta)
    return str(resp)


# ══════════════════════════════════════════════
# API endpoints (for dashboard)
# ══════════════════════════════════════════════
@app.route("/api/citas/<int:negocio_id>")
def api_citas(negocio_id):
    fecha_str = request.args.get("fecha")
    if fecha_str:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    else:
        fecha = datetime.now().date()
    citas = obtener_citas_dia(negocio_id, fecha)
    # Convert to serializable format
    result = []
    for c in citas:
        result.append({
            "id": c["id"],
            "nombre_cliente": c["nombre_cliente"],
            "telefono_cliente": c["telefono_cliente"],
            "fecha": c["fecha"].isoformat(),
            "hora_inicio": c["hora_inicio"].strftime("%I:%M %p"),
            "hora_fin": c["hora_fin"].strftime("%I:%M %p"),
            "estado": c["estado"],
            "notas": c["notas"],
        })
    return jsonify(result)


@app.route("/api/citas/<int:cita_id>/estado", methods=["POST"])
def api_cambiar_estado(cita_id):
    data = request.json
    nuevo_estado = data.get("estado")
    if nuevo_estado not in ("confirmada", "completada", "cancelada", "no_show"):
        return jsonify({"error": "Estado inválido"}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE citas SET estado = %s WHERE id = %s", (nuevo_estado, cita_id))

    # If cancelled, notify client via WhatsApp
    if nuevo_estado == "cancelada":
        c.execute("SELECT telefono_cliente, negocio_id FROM citas WHERE id = %s", (cita_id,))
        cita = c.fetchone()
        if cita:
            c.execute("SELECT nombre FROM negocios WHERE id = %s", (cita["negocio_id"],))
            neg = c.fetchone()
            try:
                twilio_client.messages.create(
                    body=f"Tu cita #{cita_id} en {neg['nombre']} ha sido cancelada. Disculpa el inconveniente.",
                    from_=TWILIO_NUMBER,
                    to=cita["telefono_cliente"]
                )
            except Exception as e:
                print(f"Error sending cancellation: {e}")

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/servicios/<int:negocio_id>")
def api_servicios(negocio_id):
    servicios = obtener_servicios(negocio_id)
    return jsonify([dict(s) for s in servicios])


@app.route("/api/stats/<int:negocio_id>")
def api_stats(negocio_id):
    conn = get_db()
    c = conn.cursor()
    hoy = datetime.now().date()

    c.execute("SELECT COUNT(*) as total FROM citas WHERE negocio_id = %s AND fecha = %s AND estado != 'cancelada'", (negocio_id, hoy))
    hoy_total = c.fetchone()["total"]

    c.execute("SELECT COUNT(*) as total FROM citas WHERE negocio_id = %s AND fecha = %s AND estado = 'completada'", (negocio_id, hoy))
    completadas = c.fetchone()["total"]

    c.execute("SELECT COUNT(*) as total FROM citas WHERE negocio_id = %s AND fecha = %s AND estado = 'cancelada'", (negocio_id, hoy))
    canceladas = c.fetchone()["total"]

    c.execute("SELECT COUNT(*) as total FROM citas WHERE negocio_id = %s AND fecha = %s AND estado = 'no_show'", (negocio_id, hoy))
    no_shows = c.fetchone()["total"]

    c.execute("""
        SELECT COALESCE(SUM(s.precio), 0) as total
        FROM citas c JOIN servicios s ON c.servicio_id = s.id
        WHERE c.negocio_id = %s AND c.fecha = %s AND c.estado = 'completada'
    """, (negocio_id, hoy))
    ingresos = float(c.fetchone()["total"])

    conn.close()
    return jsonify({
        "hoy_total": hoy_total,
        "completadas": completadas,
        "canceladas": canceladas,
        "no_shows": no_shows,
        "ingresos": ingresos,
    })


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
