# MEMORY.md — SAVANT
**Self-Aware Voice Agent for Network Tasks**
**Hackathon IApreneur × Hostinger — Deadline : 4 juin 2026**
**VPS : srv1101276.hstgr.cloud — 72.61.101.44**
**URL production : https://savant.ali-n8n.com**

---

## 1. Vision Produit

SAVANT est un **agent vocal Speech-to-Speech personnel**, hébergé sur un VPS Hostinger, accessible depuis n'importe quel navigateur (PC, mobile, tablette) via une PWA — sans installation. Il écoute, comprend, agit sur l'infrastructure, et se souvient de tout entre les sessions.

SAVANT n'est pas un assistant générique. C'est un **agent DevOps vocal** : il connaît ton VPS, tes containers Docker, tes projets. Il peut diagnostiquer une panne, redémarrer un service, créer une tâche Notion, se souvenir d'une décision prise il y a 3 semaines — tout ça à la voix, en quelques secondes.

**Règle absolue** : SAVANT ne s'exécute jamais lui-même. Il propose, le backend valide, Hermes exécute. Les actions critiques (restart container, suppression) exigent une confirmation PIN explicite avant lancement.

---

## 2. Use Cases Démo Hackathon (ordre de présentation)

**Use Case 1 — Runbook Incident** (le plus impressionnant — à montrer en premier)
L'utilisateur dit : *"SAVANT, le site est down."*
SAVANT interroge le VPS via Hermes (CPU, RAM, état Docker, logs), diagnostique vocalement et affiche les données brutes dans le panneau droit de la PWA, propose une action corrective, demande confirmation vocale, exécute. Action réelle sur infrastructure réelle, visible en direct.

**Use Case 2 — Mémoire Contextuelle** (effet waouh jury)
L'utilisateur dit : *"SAVANT, c'était quoi la décision sur le projet X ?"*
SAVANT interroge SQLite et répond vocalement avec le contexte exact — qui a décidé quoi, quand. Prouve que SAVANT se souvient vraiment entre les sessions.

**Use Case 3 — Web Search Vocal** (le plus simple, natif Gemini)
L'utilisateur dit : *"SAVANT, les dernières news en IA aujourd'hui."*
SAVANT cherche, résume vocalement, affiche les résultats dans le panneau droit. Géré nativement par Gemini.

**Use Case 4 — Dispatcher Tâches** (intégration externe)
L'utilisateur dit : *"SAVANT, crée une tâche : revoir la config SSL demain."*
SAVANT pousse vers Notion via Hermes et confirme vocalement.

---

## 3. Stack Technique Complète

**PWA** — `pwa/index.html`, fichier unique vanilla HTML/CSS/JS, servi par Caddy en statique. Capture audio via `getUserMedia()` → AudioWorklet en PCM 16-bit mono 16kHz → chunks de 3200 bytes (100ms) → WebSocket binaire vers le backend. Reçoit en retour des chunks audio PCM 24kHz (joués via AudioContext à 24000 Hz) et des frames JSON (transcripts, logs, turn_complete). Half-duplex : micro silencé pendant que SAVANT parle (flag `micMuted`), rétabli au `turn_complete`.

**Backend Python** — FastAPI + WebSockets, `main.py` (fichier unique ~800 lignes). Pont PWA ↔ Gemini Live, intercepteur tool calls, lecteur/écrivain SQLite. La clé API Gemini ne sort jamais du backend. Module `backend/memory/database.py` gère le SQLite ; `backend/hermes/client.py` appelle Hermes.

**Gemini 3.1 Flash Live Preview** — modèle `models/gemini-3.1-flash-live-preview`, SDK `google-genai` version 2.4.0. Audio-only (`response_modalities=["AUDIO"]`). Reconnexion automatique sur `GoAway` avec session resumption. Compression de contexte via `SlidingWindow`. Google Search natif activé.

**Hermes v2** — container `hermes-v2` (`nousresearch/hermes-agent`), port `8643` (host) → `8642` (container). Agent LLM autonome via OpenRouter. Exécute des commandes bash sur le VPS via gateway OpenAI-compatible. Tourne en `privileged: true` avec `pid: host` et volume `/:/host` pour accès VPS complet. Données persistantes dans `./hermes/` (root-owned). Skills custom : `vps-map`, `backup-scope`, `savant-executor`.

**Redis** — container `redis-container:6379` existant, **avec mot de passe** (stocké dans `REDIS_PASSWORD` dans `.env`). Utilisé pour l'état session temps réel : `CURRENT_TASK`, `STOP_FLAG`, `PENDING_CONFIRMATION`. Les données ne persistent pas entre sessions — c'est voulu.


**SQLite** — fichier `/home/savant/memory/savant.db` sur le VPS (hors Docker, hors Git). Mémoire longue permanente. Chargé dans le system prompt Gemini à chaque ouverture de session. Résumé automatique sauvegardé à chaque fermeture.

**Caddy** — container `caddy-proxy` existant. Gère TLS automatique (Let's Encrypt). Caddyfile à `/root/n8n-automation/Caddyfile`. Le bloc `savant.ali-n8n.com` est déjà configuré avec les trois routes : `/ws` (WebSocket backend), `/health*` (HTTP backend), `/*` (fichiers statiques PWA). Header `Permissions-Policy: microphone=(self)` configuré — obligatoire pour `getUserMedia()`.

**DNS** — géré par **Cloudflare** (pas Hostinger). Record `A savant → 72.61.101.44`, proxy **désactivé** (nuage gris, DNS only). Le proxy Cloudflare orange est incompatible avec les WebSockets longue durée sur le plan Free.

---

## 4. Architecture du Double WebSocket

C'est le point le plus important à comprendre pour ne pas se tromper :

```
Navigateur (PWA)
    ↕ wss://savant.ali-n8n.com/ws   ← WebSocket 1, chiffré TLS par Caddy
Caddy (déchiffre TLS)
    ↕ ws://savant-backend:8000/ws   ← même WebSocket 1, réseau Docker interne
FastAPI backend (main.py)
    ↕ wss://generativelanguage.googleapis.com  ← WebSocket 2, géré par SDK google-genai
Gemini Live API (Google)
```

FastAPI est le pont. Il n'y a pas de connexion directe navigateur ↔ Gemini — tout passe par le backend. C'est intentionnel : la clé API reste côté serveur, et le backend peut intercepter les tool calls avant exécution.

---

## 5. Règles de Contrôle des Actions

Les **actions read-only** (logs, stats, mémoire, news) s'exécutent sans confirmation et peuvent être stoppées via `STOP_FLAG` Redis.

Les **actions write légères** (créer une tâche Notion, sauvegarder une note) s'exécutent sans confirmation mais sont stoppables avant envoi webhook.

Les **actions critiques** (stop/delete containers, modifier Caddyfile, supprimer données) exigent une confirmation via **modal PIN** dans la PWA. Le modal s'affiche en orange, l'utilisateur entre son code d'accès (SHA-256 avant envoi). SAVANT reçoit `hermes_approve` ou `hermes_deny` du backend, puis exécute ou annule. Timeout 120 secondes.

---

## 6. Structure des Dossiers VPS

```
/home/savant/
├── app/                      ← repo Git (code source)
│   ├── .env                  ← secrets (jamais dans Git)
│   ├── .env.example          ← template (dans Git)
│   ├── hermes-agent.env      ← secrets Hermes (jamais dans Git)
│   ├── CLAUDE.md             ← contexte pour Claude Code
│   ├── MEMORY.md             ← ce fichier
│   ├── docker-compose.yml
│   ├── Dockerfile
│   ├── hermes-entrypoint.sh  ← entrypoint custom hermes-v2
│   ├── main.py               ← backend (~800 lignes, fichier unique)
│   ├── backend/
│   │   ├── memory/database.py ← SQLite read/write
│   │   └── hermes/client.py   ← appels HTTP vers Hermes
│   ├── pwa/
│   │   └── index.html
│   ├── scripts/
│   │   ├── reset_memory.sh   ← vide SQLite
│   │   ├── reset_hermes.sh   ← vide hermes vps_map/sessions/memories
│   │   ├── reset_all.sh      ← reset_memory + reset_hermes
│   │   └── deploy_hermes_savant.sh ← génère hermes-agent.env
│   ├── hermes/               ← volume Hermes (root-owned, gitignored)
│   │   ├── SOUL.md
│   │   ├── config.yaml
│   │   ├── skills/
│   │   └── vps_map/BLUEPRINT.md
│   ├── test_gemini.py
│   ├── test_websocket.py
│   └── VPS_Report.md
├── memory/                   ← hors Git (owner: savant)
│   └── savant.db
├── logs/                     ← hors Git
└── backups/hermes/           ← sauvegardes Hermes (format: YYYY-MM-DD_HHhMM_<target>.zip)
```

---

## 7. Variables d'Environnement

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Clé API Google AI Studio |
| `REDIS_HOST` | Nom du container Redis (`redis-container`) |
| `REDIS_PORT` | Port Redis (`6379`) |
| `REDIS_PASSWORD` | Mot de passe Redis (**présent** — ne pas oublier) |
| `SAVANT_PORT` | Port backend (`8000`) |
| `HERMES_URL` | URL gateway Hermes (défaut `http://hermes-v2:8642`) |
| `HERMES_WEBHOOK_SECRET` | Bearer token Hermes (doit correspondre à `API_SERVER_KEY` dans `hermes-agent.env`) |
| `OPENROUTER_API_KEY` | Clé OpenRouter pour Hermes (stockée dans `hermes-agent.env`, pas `.env`) |

---

## 8. État du Projet au 22 mai 2026

**DONE et VALIDÉ EN PRODUCTION :**

- Infrastructure VPS (Docker, Redis, Caddy) — existante et opérationnelle
- DNS `savant.ali-n8n.com` → `72.61.101.44` configuré dans Cloudflare (DNS only)
- Gemini Live (`/ws`) — pipeline audio PCM complet, reconnexion auto sur GoAway, session resumption
- SQLite memory — `database.py` complet (save/load/sessions/is_first_run)
- Identity gate — code d'accès SHA-256, premier run → sauvegarde + onboarding, reconnexion → vérification
- Onboarding 5 questions — strict mode, redirection hors-sujet, injection mémoire après `owner_name`
- Hermes v2 — container séparé, privileged + pid:host + /:/host, gateway OpenAI-compatible sur port 8643
- `execute_action` non-bloquant — `pending` immédiat, résultat injecté via `[HERMES RESULT]`, GoAway-safe
- Actions critiques — modal PIN orange dans la PWA, SHA-256 avant envoi, timeout 120s
- `show_data` tool — push de cartes données dans le panneau DATA de la PWA
- PWA complète — status bar Hermes, panneau DATA/LOGS, modal PIN, waveform, session history, beeps
- Scripts — `reset_memory.sh`, `reset_hermes.sh`, `reset_all.sh`, `deploy_hermes_savant.sh`
- Session recording — auto-summary sur déconnexion browser si transcript non vide
- Session init — séquence 3 étapes après verification (load blueprint → vps-map si absent → greet)

**À FAIRE avant hackathon (4 juin 2026) :**

1. Demo prep — script exact, données SQLite, répétitions use cases
2. Tester les 4 use cases en conditions réelles (Runbook, Mémoire, Web Search, Dispatcher)
3. apt upgrade VPS (10 updates en attente, kernel update requis)
4. Vérifier hermes skills (vps-map, backup-scope) en production
5. (Optionnel) Dispatcher Tâches Notion via Hermes si temps disponible

---

## 9. Contraintes Absolues

Ces règles ne sont jamais négociables, quel que soit le contexte :

- Ne **jamais** committer `.env` ou `*.db` dans Git
- Ne **jamais** exposer `GEMINI_API_KEY` côté PWA ou dans les logs
- Tout le code et les commentaires sont en **anglais**
- Les actions critiques (restart container, suppression) exigent une **confirmation PIN** avant exécution
- Le réseau Docker interne s'appelle `n8n-automation_n8n_network` (nom hérité de l'infra VPS) — ne pas en créer un autre

---

## 10. Commandes Utiles de Référence

```bash
# Démarrer/arrêter SAVANT + Hermes
cd /home/savant/app
docker compose up -d
docker compose down
docker compose logs -f savant-backend
docker compose logs -f hermes-v2

# Rebuild après modif main.py (OBLIGATOIRE — restart ne recompile pas)
docker compose build && docker compose up -d

# Healthchecks
curl http://localhost:8000/health
curl http://localhost:8000/health/redis
curl http://localhost:8000/health/sessions
curl http://localhost:8643/health   # Hermes gateway

# Tester Hermes directement
curl -s -X POST http://localhost:8643/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <HERMES_WEBHOOK_SECRET>" \
  -d '{"model":"hermes-agent","messages":[{"role":"user","content":"Run: df -h"}]}'

# Reset
bash /home/savant/app/scripts/reset_memory.sh   # SQLite seulement
bash /home/savant/app/scripts/reset_hermes.sh   # Hermes seulement
bash /home/savant/app/scripts/reset_all.sh      # Les deux (first-run complet)

# Inspecter la mémoire SQLite
docker exec savant-backend sqlite3 /app/memory/savant.db \
  "SELECT key, value FROM memory ORDER BY updated_at DESC;"

# Supprimer une clé (ex: action_code pour retester l'onboarding)
docker exec savant-backend sqlite3 /app/memory/savant.db \
  "DELETE FROM memory WHERE key='action_code';"

# Recharger Caddy après modif Caddyfile (TOUJOURS faire les deux)
docker exec caddy-proxy caddy fmt --overwrite /etc/caddy/Caddyfile
docker exec caddy-proxy caddy reload --config /etc/caddy/Caddyfile

# Vérifier Redis
docker exec redis-container redis-cli -a $REDIS_PASSWORD ping

# Voir tous les containers
docker ps -a

# Git workflow
git add -A && git commit -m "feat: ..." && git push origin dev
```

---

*MEMORY.md créé le 19 mai 2026 — mis à jour le 22 mai 2026 — à maintenir à jour après chaque session de développement.*