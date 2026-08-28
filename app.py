
import re
import uuid
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

st.set_page_config(
    page_title="Family Food Planner",
    page_icon="🥑",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
.block-container {max-width: 780px; padding-top: 1.2rem; padding-bottom: 4rem;}
h1 {font-size: 1.9rem !important;}
div.stButton > button,
div[data-testid="stFormSubmitButton"] button {
    width: 100%; min-height: 46px; border-radius: 12px; font-weight: 700;
}
.question-card {
    padding: 12px 14px;
    border: 1px solid rgba(128,128,128,.24);
    border-radius: 14px;
    margin-bottom: 12px;
}
[data-testid="stProgress"] {margin-bottom: .6rem;}
.small {opacity: .70; font-size: .88rem;}
</style>
""", unsafe_allow_html=True)

# ============================================================
# CONFIG / GOOGLE SHEETS
# ============================================================

REQUIRED_SHEETS = ["Usuarios", "Cuestionario_V1", "Opciones_V1", "Encuestas", "Respuestas"]

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def spreadsheet_id_from_secret(value: str) -> str:
    value = str(value).strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", value)
    return m.group(1) if m else value

@st.cache_resource
def sheets_service():
    cfg = dict(st.secrets["connections"]["gsheets"])
    spreadsheet_secret = cfg.pop("spreadsheet", cfg.pop("spreadsheet_id", None))
    if not spreadsheet_secret:
        raise RuntimeError("Falta 'spreadsheet' o 'spreadsheet_id' en Secrets.")

    # TOML can preserve the PEM newlines, but this also supports escaped \n.
    if "private_key" in cfg:
        cfg["private_key"] = str(cfg["private_key"]).replace("\\n", "\n")

    creds = service_account.Credentials.from_service_account_info(
        cfg,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return service, spreadsheet_id_from_secret(spreadsheet_secret)

def api():
    return sheets_service()

@st.cache_data(ttl=30)
def read_table(sheet_name: str) -> pd.DataFrame:
    service, spreadsheet_id = api()
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A:ZZ",
    ).execute()
    values = result.get("values", [])
    if not values:
        return pd.DataFrame()

    headers = [str(x).strip() for x in values[0]]
    rows = []
    for row in values[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        rows.append(padded[:len(headers)])
    return pd.DataFrame(rows, columns=headers)

def clear_read_cache():
    read_table.clear()

def append_dict_rows(sheet_name: str, row_dicts: list[dict]):
    if not row_dicts:
        return
    service, spreadsheet_id = api()

    headers_df = read_table(sheet_name)
    headers = list(headers_df.columns)
    if not headers:
        raise RuntimeError(f"La pestaña {sheet_name} no tiene encabezados.")

    values = [[row.get(h, "") for h in headers] for row in row_dicts]
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    clear_read_cache()

def column_letter(n: int) -> str:
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s

def mark_survey_complete(survey_id: str):
    """Targeted update of the completion cell; does not rewrite the worksheet."""
    service, spreadsheet_id = api()
    df = read_table("Encuestas")
    if df.empty or "survey_id" not in df.columns or "completada" not in df.columns:
        raise RuntimeError("Encuestas requiere columnas survey_id y completada.")

    matches = df.index[df["survey_id"].astype(str) == str(survey_id)].tolist()
    if not matches:
        raise RuntimeError("No encontré el survey_id para marcarlo como completado.")

    # DataFrame row 0 is Sheets row 2 because row 1 contains headers.
    sheet_row = matches[-1] + 2
    col_num = list(df.columns).index("completada") + 1
    target = f"'Encuestas'!{column_letter(col_num)}{sheet_row}"

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=target,
        valueInputOption="USER_ENTERED",
        body={"values": [[True]]},
    ).execute()
    clear_read_cache()

def healthcheck():
    """Fast schema check shown as a friendly error instead of a traceback."""
    missing = []
    for s in REQUIRED_SHEETS:
        df = read_table(s)
        if len(df.columns) == 0:
            missing.append(s)
    return missing

# ============================================================
# OPTIONAL FAMILY PIN
# ============================================================

def require_family_pin():
    expected = str(st.secrets.get("APP_PIN", "")).strip()
    if not expected:
        return

    if st.session_state.get("pin_ok"):
        return

    st.title("🥑 Family Food Planner")
    st.write("Ingresa el PIN familiar para continuar.")
    entered = st.text_input("PIN", type="password")
    if st.button("Entrar"):
        if entered == expected:
            st.session_state.pin_ok = True
            st.rerun()
        else:
            st.error("PIN incorrecto.")
    st.stop()

require_family_pin()

# ============================================================
# LOAD MASTER DATA
# ============================================================

try:
    missing = healthcheck()
    if missing:
        st.error("No pude leer correctamente estas pestañas: " + ", ".join(missing))
        st.stop()

    usuarios = read_table("Usuarios")
    preguntas = read_table("Cuestionario_V1")
    opciones = read_table("Opciones_V1")
except (KeyError, HttpError, RuntimeError) as exc:
    st.error("No fue posible conectar con Google Sheets.")
    st.code(str(exc))
    st.stop()

SECTIONS = [
    "Perfil",
    "Ingredientes",
    "Recetas",
    "Preferencias generales",
    "Comparaciones",
    "Actividad física",
    "Logística padres",
]
CHUNK = 10

# ============================================================
# HELPERS
# ============================================================

def truthy(v):
    return str(v).strip().lower() in {"true", "1", "sí", "si", "yes"}

def get_user(uid):
    rows = usuarios[usuarios["user_id"].astype(str) == str(uid)]
    return None if rows.empty else rows.iloc[0]

def audience_allowed(audience, role):
    audience = str(audience).strip()
    return (
        audience == "Todos"
        or (audience == "Adolescentes" and role == "Adolescente")
        or (audience == "Padres" and role == "Padre/Madre")
    )

def visible_questions(uid):
    user = get_user(uid)
    role = str(user["rol"])
    return preguntas[
        preguntas.apply(lambda r: audience_allowed(r["audiencia"], role), axis=1)
    ].copy()

def question_options(qid):
    return opciones[opciones["question_id"].astype(str) == str(qid)].copy()

def incomplete_survey_for_user(uid):
    df = read_table("Encuestas")
    if df.empty:
        return None

    x = df[df["user_id"].astype(str) == str(uid)].copy()
    if x.empty:
        return None

    complete = x["completada"].astype(str).str.lower().isin({"true", "1", "sí", "si"})
    x = x.loc[~complete]
    if x.empty:
        return None

    if "fecha_hora" in x.columns:
        x["_dt"] = pd.to_datetime(x["fecha_hora"], errors="coerce", utc=True)
        x = x.sort_values("_dt")
    return x.iloc[-1]

def latest_answers(survey_id):
    df = read_table("Respuestas")
    if df.empty:
        return {}

    x = df[df["survey_id"].astype(str) == str(survey_id)].copy()
    if x.empty:
        return {}

    if "timestamp" in x.columns:
        x["_dt"] = pd.to_datetime(x["timestamp"], errors="coerce", utc=True)
        x = x.sort_values("_dt")
    x = x.drop_duplicates(subset=["question_id"], keep="last")

    result = {}
    for _, r in x.iterrows():
        result[str(r["question_id"])] = {
            "question_id": str(r["question_id"]),
            "option_id": str(r.get("option_id", "")),
            "label": str(r.get("valor_texto", "")),
            "score": r.get("score", ""),
        }
    return result

def start_new_survey(uid):
    user = get_user(uid)
    survey_id = "S_" + uuid.uuid4().hex[:12].upper()
    append_dict_rows("Encuestas", [{
        "survey_id": survey_id,
        "user_id": str(uid),
        "fecha_hora": now_utc(),
        "version": "V1.1",
        "tipo_encuesta": "baseline",
        "completada": False,
    }])
    enter_survey(uid, survey_id)

def enter_survey(uid, survey_id):
    user = get_user(uid)
    st.session_state.mode = "survey"
    st.session_state.user_id = str(uid)
    st.session_state.user_name = str(user["nombre_visible"])
    st.session_state.survey_id = str(survey_id)
    st.session_state.answers = latest_answers(survey_id)
    jump_to_first_unanswered()
    st.rerun()

def reset_to_home():
    for key in list(st.session_state.keys()):
        if key.startswith("w_") or key.startswith("sub_"):
            del st.session_state[key]
    for key in ["mode", "user_id", "user_name", "survey_id", "answers", "step", "finished"]:
        st.session_state.pop(key, None)
    st.rerun()

def active_sections_for_user(uid):
    q = visible_questions(uid)
    return [s for s in SECTIONS if not q[q["seccion"] == s].empty]

def jump_to_first_unanswered():
    uid = st.session_state.user_id
    answered = set(st.session_state.get("answers", {}).keys())
    q = visible_questions(uid)
    sections = active_sections_for_user(uid)

    st.session_state.step = 0
    for s_idx, section in enumerate(sections):
        qsec = q[q["seccion"] == section].copy()
        qids = qsec["question_id"].astype(str).tolist()
        for pos, qid in enumerate(qids):
            if qid not in answered:
                st.session_state.step = s_idx
                st.session_state[f"sub_{section}"] = pos // CHUNK
                return

    # Everything answered but not marked complete.
    st.session_state.step = max(0, len(sections) - 1)
    last = sections[-1]
    qsec = q[q["seccion"] == last]
    st.session_state[f"sub_{last}"] = max(0, (len(qsec)-1) // CHUNK)

def save_visible_answers(answer_dict):
    now = now_utc()
    rows = []
    for ans in answer_dict.values():
        rows.append({
            "survey_id": st.session_state.survey_id,
            "user_id": st.session_state.user_id,
            "question_id": ans["question_id"],
            "option_id": ans["option_id"],
            "valor_texto": ans["label"],
            "score": ans["score"],
            "timestamp": now,
        })
    # Append-only event log: safest for concurrent users and preserves edits/history.
    append_dict_rows("Respuestas", rows)
    st.session_state.answers.update(answer_dict)

def progress_stats(uid, survey_id):
    q = visible_questions(uid)
    answered = latest_answers(survey_id)
    total = len(q)
    done = len(set(q["question_id"].astype(str)) & set(answered.keys()))
    return done, total

# ============================================================
# HOME / USER SELECTION
# ============================================================

if st.session_state.get("mode") != "survey":
    st.title("🥑 Family Food Planner")
    st.write("Queremos saber qué desayunos y lunches realmente funcionan para ti.")
    st.caption("No contestes lo que 'deberías' comer. Contesta lo que realmente comerías.")

    active = usuarios[usuarios["activo"].apply(truthy)].copy()

    cols = st.columns(2)
    for i, (_, row) in enumerate(active.iterrows()):
        uid = str(row["user_id"])
        name = str(row["nombre_visible"])
        with cols[i % 2]:
            if st.button(name, key=f"choose_{uid}"):
                st.session_state["chosen_uid"] = uid
                st.rerun()

    chosen = st.session_state.get("chosen_uid")
    if chosen:
        user = get_user(chosen)
        incomplete = incomplete_survey_for_user(chosen)
        st.divider()
        st.subheader(str(user["nombre_visible"]))

        if incomplete is not None:
            sid = str(incomplete["survey_id"])
            done, total = progress_stats(chosen, sid)
            pct = int(round(100 * done / max(1, total)))
            st.info(f"Tienes una encuesta sin terminar: **{done}/{total} preguntas ({pct}%)**.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Continuar encuesta", type="primary"):
                    enter_survey(chosen, sid)
            with c2:
                if st.button("Empezar una nueva"):
                    start_new_survey(chosen)
        else:
            if st.button("Comenzar encuesta", type="primary"):
                start_new_survey(chosen)

        if st.button("← Elegir otra persona"):
            st.session_state.pop("chosen_uid", None)
            st.rerun()

    st.stop()

# ============================================================
# SURVEY UI
# ============================================================

uid = st.session_state.user_id
all_q = visible_questions(uid)
sections = active_sections_for_user(uid)

step = min(int(st.session_state.get("step", 0)), len(sections)-1)
section = sections[step]
q_section = all_q[all_q["seccion"] == section].copy()

subkey = f"sub_{section}"
if subkey not in st.session_state:
    st.session_state[subkey] = 0

subpages = max(1, (len(q_section) + CHUNK - 1) // CHUNK)
sub = min(int(st.session_state[subkey]), subpages - 1)
start = sub * CHUNK
end = min(len(q_section), start + CHUNK)
q_display = q_section.iloc[start:end]

done, total = progress_stats(uid, st.session_state.survey_id)
st.caption(f"Contestando como **{st.session_state.user_name}** · {done}/{total} guardadas")
st.progress(done / max(1, total), text=f"{section} · bloque {sub+1} de {subpages}")

if section == "Ingredientes":
    st.title("¿Qué alimentos sí van contigo?")
    st.write("**1 = no lo como** y **5 = me encanta**. Un 1 se tratará como veto.")
elif section == "Recetas":
    st.title("¿Qué tanto te gustaría esto?")
    st.write("Piensa en si realmente te lo comerías, no en si suena saludable.")
else:
    st.title(section)

current = {}

with st.form(f"form_{section}_{sub}"):
    for _, qrow in q_display.iterrows():
        qid = str(qrow["question_id"])
        qtext = str(qrow["pregunta"])
        qtype = str(qrow["tipo_respuesta"])
        opts = question_options(qid)

        labels = opts["etiqueta"].astype(str).tolist()
        row_by_label = {str(r["etiqueta"]): r for _, r in opts.iterrows()}

        saved = st.session_state.answers.get(qid)
        saved_label = saved.get("label") if saved else None
        default_index = labels.index(saved_label) if saved_label in labels else None

        st.markdown(f'<div class="question-card"><b>{qtext}</b>', unsafe_allow_html=True)
        selected = st.radio(
            qtext,
            labels,
            index=default_index,
            horizontal=(qtype in {"preference_1_5", "scale_1_5"}),
            key=f"w_{qid}",
            label_visibility="collapsed",
        )
        st.markdown("</div>", unsafe_allow_html=True)

        if selected is None:
            current[qid] = None
        else:
            r = row_by_label[selected]
            score = r.get("score", "")
            current[qid] = {
                "question_id": qid,
                "option_id": str(r.get("option_id", "")),
                "label": str(selected),
                "score": "" if pd.isna(score) else score,
            }

    c1, c2 = st.columns(2)
    with c1:
        back = st.form_submit_button("← Atrás")
    with c2:
        final_page = step == len(sections)-1 and sub == subpages-1
        nxt = st.form_submit_button(
            "Terminar encuesta ✓" if final_page else "Guardar y continuar →"
        )

if back:
    if sub > 0:
        st.session_state[subkey] = sub - 1
    elif step > 0:
        st.session_state.step = step - 1
        prev_section = sections[step - 1]
        prev_q = all_q[all_q["seccion"] == prev_section]
        st.session_state[f"sub_{prev_section}"] = max(0, (len(prev_q)-1) // CHUNK)
    else:
        reset_to_home()
    st.rerun()

if nxt:
    missing = [qid for qid, ans in current.items() if ans is None]
    if missing:
        st.error("Contesta todas las preguntas visibles antes de continuar.")
        st.stop()

    try:
        save_visible_answers(current)
    except HttpError as exc:
        st.error("No pude guardar este bloque. Tus selecciones siguen en pantalla; vuelve a intentar.")
        st.code(str(exc))
        st.stop()

    if sub < subpages - 1:
        st.session_state[subkey] = sub + 1
    elif step < len(sections) - 1:
        st.session_state.step = step + 1
    else:
        try:
            mark_survey_complete(st.session_state.survey_id)
            st.session_state.finished = True
        except HttpError as exc:
            st.error("Las respuestas se guardaron, pero faltó marcar la encuesta como terminada.")
            st.code(str(exc))
            st.stop()
    st.rerun()

if st.session_state.get("finished"):
    st.balloons()
    st.success("¡Listo! Tu encuesta quedó guardada.")
    if st.button("Volver al inicio"):
        reset_to_home()
