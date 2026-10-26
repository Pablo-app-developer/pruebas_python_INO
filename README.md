# Pruebas de Python

Plataforma web para aplicar pruebas de Python con terminal interactiva en el navegador (Pyodide).

## Local

    pip install flask
    python app.py        # imprime la URL y la clave del panel /admin

## Despliegue en Vercel

1. Importa este repositorio en Vercel (Flask se detecta con `app.py`).
2. En **Storage** agrega una base Postgres (por ejemplo Neon). Esto crea `DATABASE_URL`.
3. Define estas variables de entorno:
   - `ADMIN_PASS`: clave del panel `/admin` (usuario: cualquiera).
   - `CANDIDATOS` (opcional): nombres separados por `;`; se crean solos la primera vez.
4. Redespliega y abre `/admin` para copiar el enlace personal de cada candidato.

Las preguntas están en `questions.json` (o en la variable `QUESTIONS_JSON`).
