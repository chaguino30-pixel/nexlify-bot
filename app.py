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
- NUNCA repitas el mismo nombre dos veces en una confirmación.
- REGLA CRÍTICA DE HORARIOS: Solo hay UN profesional atendiendo. NUNCA pongas dos citas a la misma hora. Si agendan 2+ personas, SIEMPRE escalonar: si el primer servicio dura 30min y empieza a las 3:00, el segundo empieza a las 3:30. Si dura 45min y empieza a las 3:00, el segundo a las 3:45. Ejemplo correcto para 2 cortes (30min c/u) a partir de las 3:00 PM:
  Persona 1 → 03:00 PM
  Persona 2 → 03:30 PM

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
        bloques = texto_respuesta.split("CITA CONFIRMADA")
        citas_creadas = []
        ya_procesadas = set()  # Track name+date+time to prevent in-batch duplicates

        for bloque in bloques[1:]:
            datos = parsear_confirmacion("CITA CONFIRMADA" + bloque)
            if datos.get("nombre") and datos.get("servicio") and datos.get("hora"):
                servicio = encontrar_servicio(servicios, datos["servicio"])
                if servicio:
                    fecha = parsear_fecha(datos.get("fecha", ""), tz) if datos.get("fecha") else ahora.date()
                    hora_inicio = parsear_hora(datos["hora"])
                    if hora_inicio:
                        # In-batch dedup key
                        clave = f"{datos['nombre'].lower()}|{fecha}|{hora_inicio}"
                        if clave in ya_procesadas:
                            continue
                        ya_procesadas.add(clave)

                        hora_fin = (datetime.combine(fecha, hora_inicio) + timedelta(minutes=servicio["duracion_min"])).time()

                        # DB dedup check
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

@app.route("/dashboard/<slug>/config")
def config_page(slug):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM negocios WHERE slug = %s AND activo = TRUE", (slug,))
    negocio = c.fetchone()
    conn.close()
    if not negocio:
        return "Negocio no encontrado", 404
    return render_template("config.html", negocio=negocio)


# ══════════════════════════════════════════════
# Admin panel (for you — Nexlify owner)
# ══════════════════════════════════════════════
ADMIN_KEY = os.environ.get("ADMIN_KEY", "nexlify-admin-2025")

@app.route("/admin")
def admin_page():
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return "Acceso denegado. Usa /admin?key=TU_CLAVE", 403
    return render_template("admin.html", admin_key=ADMIN_KEY)

@app.route("/api/admin/negocios", methods=["GET"])
def api_admin_negocios():
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return jsonify({"error": "No autorizado"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT n.*, 
        (SELECT COUNT(*) FROM citas WHERE negocio_id = n.id AND estado = 'confirmada' AND fecha >= CURRENT_DATE) as citas_pendientes,
        (SELECT COUNT(*) FROM citas WHERE negocio_id = n.id) as citas_total
        FROM negocios n ORDER BY n.id""")
    negocios = c.fetchall()
    conn.close()
    result = []
    for n in negocios:
        result.append({
            "id": n["id"], "nombre": n["nombre"], "slug": n["slug"],
            "direccion": n["direccion"], "telefono_contacto": n["telefono_contacto"],
            "zona_horaria": n["zona_horaria"], "activo": n["activo"],
            "citas_pendientes": n["citas_pendientes"], "citas_total": n["citas_total"],
            "fecha_creacion": n["fecha_creacion"].isoformat() if n["fecha_creacion"] else ""
        })
    return jsonify(result)

@app.route("/api/admin/negocios", methods=["POST"])
def api_admin_crear_negocio():
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return jsonify({"error": "No autorizado"}), 403
    data = request.json
    nombre = data.get("nombre", "").strip()
    if not nombre:
        return jsonify({"error": "Nombre requerido"}), 400

    # Generate slug from name
    slug = nombre.lower().replace(" ", "-").replace("'", "")
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')

    conn = get_db()
    c = conn.cursor()

    # Check slug is unique
    c.execute("SELECT id FROM negocios WHERE slug = %s", (slug,))
    if c.fetchone():
        slug = slug + "-" + str(int(datetime.now().timestamp()) % 10000)

    c.execute("""INSERT INTO negocios (nombre, slug, direccion, telefono_contacto, zona_horaria)
        VALUES (%s, %s, %s, %s, %s) RETURNING id, slug""",
        (nombre, slug, data.get("direccion", ""), data.get("telefono_contacto", ""),
         data.get("zona_horaria", "America/Chicago")))
    row = c.fetchone()
    negocio_id = row["id"]
    slug_final = row["slug"]

    # Create default schedule (Mon-Fri 9-7, Sat 9-5)
    horarios_default = [(0,"09:00","19:00"),(1,"09:00","19:00"),(2,"09:00","19:00"),
                        (3,"09:00","19:00"),(4,"09:00","19:00"),(5,"09:00","17:00")]
    for dia, ini, fin in horarios_default:
        c.execute("INSERT INTO horarios (negocio_id, dia_semana, hora_inicio, hora_fin) VALUES (%s,%s,%s,%s)",
                  (negocio_id, dia, ini, fin))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": negocio_id, "slug": slug_final})

@app.route("/api/admin/negocios/<int:negocio_id>/toggle", methods=["POST"])
def api_admin_toggle_negocio(negocio_id):
    key = request.args.get("key", "")
    if key != ADMIN_KEY:
        return jsonify({"error": "No autorizado"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE negocios SET activo = NOT activo WHERE id = %s", (negocio_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════
# Config API endpoints
# ══════════════════════════════════════════════
@app.route("/api/negocio/<int:negocio_id>", methods=["GET"])
def api_get_negocio(negocio_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, nombre, slug, direccion, telefono_contacto, zona_horaria FROM negocios WHERE id = %s", (negocio_id,))
    neg = c.fetchone()
    conn.close()
    if not neg:
        return jsonify({"error": "No encontrado"}), 404
    return jsonify(dict(neg))

@app.route("/api/negocio/<int:negocio_id>", methods=["PUT"])
def api_update_negocio(negocio_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    c.execute("""UPDATE negocios SET nombre = %s, direccion = %s, telefono_contacto = %s
        WHERE id = %s""", (data.get("nombre"), data.get("direccion"), data.get("telefono_contacto"), negocio_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/servicios/<int:negocio_id>", methods=["POST"])
def api_add_servicio(negocio_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO servicios (negocio_id, nombre, duracion_min, precio) VALUES (%s,%s,%s,%s) RETURNING id",
              (negocio_id, data["nombre"], data["duracion_min"], data["precio"]))
    sid = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": sid})

@app.route("/api/servicios/<int:servicio_id>", methods=["PUT"])
def api_update_servicio(servicio_id):
    data = request.json
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE servicios SET nombre=%s, duracion_min=%s, precio=%s, activo=%s WHERE id=%s",
              (data["nombre"], data["duracion_min"], data["precio"], data.get("activo", True), servicio_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/servicios/<int:servicio_id>", methods=["DELETE"])
def api_delete_servicio(servicio_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE servicios SET activo = FALSE WHERE id = %s", (servicio_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/horarios/<int:negocio_id>", methods=["GET"])
def api_get_horarios(negocio_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM horarios WHERE negocio_id = %s ORDER BY dia_semana", (negocio_id,))
    rows = c.fetchall()
    conn.close()
    result = []
    for h in rows:
        result.append({
            "id": h["id"], "dia_semana": h["dia_semana"],
            "hora_inicio": h["hora_inicio"].strftime("%H:%M"),
            "hora_fin": h["hora_fin"].strftime("%H:%M"),
            "activo": h["activo"]
        })
    return jsonify(result)

@app.route("/api/horarios/<int:negocio_id>", methods=["POST"])
def api_save_horarios(negocio_id):
    data = request.json  # list of {dia_semana, hora_inicio, hora_fin, activo}
    conn = get_db()
    c = conn.cursor()
    for h in data:
        c.execute("""INSERT INTO horarios (negocio_id, dia_semana, hora_inicio, hora_fin, activo)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (negocio_id, dia_semana)
            DO UPDATE SET hora_inicio=%s, hora_fin=%s, activo=%s""",
            (negocio_id, h["dia_semana"], h["hora_inicio"], h["hora_fin"], h["activo"],
             h["hora_inicio"], h["hora_fin"], h["activo"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════
# Fase 4: Recordatorios automáticos (1 hora antes)
# ══════════════════════════════════════════════
import threading
import time

def enviar_recordatorios():
    """Check for appointments coming up in ~1 hour and send reminders."""
    while True:
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            conn.autocommit = True
            c = conn.cursor()

            # Get all active businesses
            c.execute("SELECT * FROM negocios WHERE activo = TRUE")
            negocios = c.fetchall()

            for neg in negocios:
                tz = ZoneInfo(neg["zona_horaria"])
                ahora = datetime.now(tz)
                en_1_hora = ahora + timedelta(minutes=60)

                # Find confirmed appointments between now+50min and now+65min (window to catch them once)
                c.execute("""
                    SELECT c.*, s.nombre as servicio_nombre, n.nombre as negocio_nombre
                    FROM citas c
                    JOIN servicios s ON c.servicio_id = s.id
                    JOIN negocios n ON c.negocio_id = n.id
                    WHERE c.negocio_id = %s
                    AND c.fecha = %s
                    AND c.estado = 'confirmada'
                    AND c.recordatorio_enviado = FALSE
                    AND c.hora_inicio BETWEEN %s AND %s
                """, (neg["id"], ahora.date(),
                      (ahora + timedelta(minutes=50)).time(),
                      (ahora + timedelta(minutes=70)).time()))

                citas = c.fetchall()

                for cita in citas:
                    hora_str = cita["hora_inicio"].strftime("%I:%M %p")
                    mensaje = (
                        f"⏰ Recordatorio: tu cita de {cita['servicio_nombre']} "
                        f"en {cita['negocio_nombre']} es en ~1 hora ({hora_str}).\n\n"
                        f"¡Te esperamos! Si necesitas cancelar, responde \"cancelar\"."
                    )
                    try:
                        twilio_client.messages.create(
                            body=mensaje,
                            from_=TWILIO_NUMBER,
                            to=cita["telefono_cliente"]
                        )
                        # Mark as sent
                        c.execute("UPDATE citas SET recordatorio_enviado = TRUE WHERE id = %s", (cita["id"],))
                        print(f"Reminder sent for appointment #{cita['id']} to {cita['telefono_cliente']}")
                    except Exception as e:
                        print(f"Reminder send error for #{cita['id']}: {e}")

            conn.close()
        except Exception as e:
            print(f"Reminder loop error: {e}")

        # Check every 5 minutes
        time.sleep(300)


def iniciar_recordatorios():
    """Start reminder thread as daemon."""
    t = threading.Thread(target=enviar_recordatorios, daemon=True)
    t.start()
    print("Reminder system started (checking every 5 min)")


# Manual trigger endpoint (for testing)
@app.route("/api/recordatorios/check", methods=["POST"])
def api_check_recordatorios():
    """Manually trigger reminder check (for testing)."""
    try:
        conn = get_db()
        conn.autocommit = True
        c = conn.cursor()

        enviados = 0
        c.execute("SELECT * FROM negocios WHERE activo = TRUE")
        negocios = c.fetchall()

        for neg in negocios:
            tz = ZoneInfo(neg["zona_horaria"])
            ahora = datetime.now(tz)

            c.execute("""
                SELECT c.*, s.nombre as servicio_nombre, n.nombre as negocio_nombre
                FROM citas c
                JOIN servicios s ON c.servicio_id = s.id
                JOIN negocios n ON c.negocio_id = n.id
                WHERE c.negocio_id = %s AND c.fecha = %s AND c.estado = 'confirmada'
                AND c.recordatorio_enviado = FALSE
                AND c.hora_inicio BETWEEN %s AND %s
            """, (neg["id"], ahora.date(),
                  (ahora + timedelta(minutes=50)).time(),
                  (ahora + timedelta(minutes=70)).time()))

            citas = c.fetchall()
            for cita in citas:
                hora_str = cita["hora_inicio"].strftime("%I:%M %p")
                mensaje = (
                    f"⏰ Recordatorio: tu cita de {cita['servicio_nombre']} "
                    f"en {cita['negocio_nombre']} es en ~1 hora ({hora_str}).\n\n"
                    f"¡Te esperamos! Si necesitas cancelar, responde \"cancelar\"."
                )
                try:
                    twilio_client.messages.create(body=mensaje, from_=TWILIO_NUMBER, to=cita["telefono_cliente"])
                    c.execute("UPDATE citas SET recordatorio_enviado = TRUE WHERE id = %s", (cita["id"],))
                    enviados += 1
                except Exception as e:
                    print(f"Manual reminder error: {e}")

        conn.close()
        return jsonify({"ok": True, "enviados": enviados})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════
# Init & Run
# ══════════════════════════════════════════════
with app.app_context():
    init_db()
    seed_demo()

# Start reminder thread
iniciar_recordatorios()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
