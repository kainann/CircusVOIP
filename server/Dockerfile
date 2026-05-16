# =============================================
#  CircusVOIP Server - image Docker
# =============================================
# Image legere pour heberger les deux processus serveur (positions + audio)
# sur un VPS Linux. Le client n'est PAS conteneurise (GUI Windows + capture
# d'ecran + OCR).
#
# Les donnees runtime (token, cert TLS, tickets, canaux, profils) sont
# stockees dans /data, qui doit etre monte en volume pour persister entre
# redemarrages. La generation du certificat auto-signe se fait au premier
# lancement.
# =============================================

FROM python:3.12-slim

# Pas de tkinter dans l'image : on lance toujours en --headless.
# Pas de easyocr/torch/PySide6 non plus : c'est le client qui en a besoin.

WORKDIR /app

# Installer les deps depuis pyproject.toml AVANT de copier les sources, pour
# que le layer pip soit reutilise tant que les deps ne bougent pas.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copier les sources serveur. Les fichiers .json et .pem qui pourraient
# trainer dans le dossier ne sont PAS copies (cf. .dockerignore).
COPY *.py ./

# Toutes les ecritures (cert.pem, token, tickets, channels.json, etc.) se
# font dans /data via CIRCUSVOIP_DATA_DIR. Le code source dans /app reste
# read-only.
ENV CIRCUSVOIP_DATA_DIR=/data
VOLUME ["/data"]

# Par defaut, on lance le serveur positions. docker-compose.yml override
# pour lancer aussi le serveur audio dans un second container.
CMD ["python", "circusvoip_server.py", "--headless"]
