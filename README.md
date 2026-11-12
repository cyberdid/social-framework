# Social

**Social — аналіз інформаційного простору з агентом і моделлю на ваш вибір.** Ви розмовляєте з агентом-аналітиком так само, як у чаті. Агент збирає публічні сліди, перевіряє твердження, робить висновки з ймовірностями, будує сценарії і пише зрозумілу довідку.

Думає агент: Claude Code, Codex, Antigravity або opencode, з будь-якою моделлю, яку він підтримує. Social дає йому:
- **теку кейсу**, де зберігається весь стан, тож агента можна змінити посеред роботи;
- **методологію** у вигляді рекомендацій;
- **інструменти через MCP**: збір із Telegram, YouTube і Google News, архів сторінок, фактчеки, власні висновки, сценарії з оновленням за Баєсом і обліком влучань, збирання звіту.

## Швидкий старт

1. Встановіть хоча б одного агента і увійдіть у нього (ваша підписка або безкоштовні моделі):
   - [Claude Code](https://docs.claude.com/en/docs/claude-code) — `claude`;
   - [Codex CLI](https://github.com/openai/codex) — `codex`;
   - Antigravity CLI — `agy`;
   - [opencode](https://opencode.ai) — `opencode`, має безкоштовні й локальні моделі.
2. Встановіть Social (потрібен [uv](https://docs.astral.sh/uv/)):

   ```sh
   uv tool install "social-framework[youtube] @ git+https://github.com/cyberdid/social-framework"
   social doctor            # що встановлено
   social backends --models # агенти і їхні моделі
   ```

3. Створіть кейс і почніть розмову:

   ```sh
   social new "Назва кейсу" --question "Що саме хочемо з'ясувати?"
   cd ~/SocialCases/назва-кейсу
   social chat                                      # аналітик за замовчуванням: Claude Code + Opus 5.5
   social chat --agent codex --model <модель>       # або інший агент і модель
   social chat --agent agy --model gemini-4-argon-high
   social chat --agent opencode --model opencode/big-pickle
   ```

   Для Antigravity один раз виконайте `social setup agy`: так він дізнається про інструменти Social.

### Веб-інтерфейс

```sh
social web        # відкриє браузер: кейси, чат з будь-яким агентом, фактчеки, висновки, сценарії, дані, звіт
```

Інтерфейс працює лише на вашому комп'ютері (127.0.0.1) і відкривається за посиланням з одноразовим ключем.

Що в ньому є:
- **Чат зі стрімінгом відповіді.** Видно, які інструменти агент викликає. Агента й модель обираєте для кожної розмови.
- **«Червона команда».** Один клік, і агент іншого виробника читає кейс і шукає слабкі місця, нічого не змінюючи.
- **Наочні вкладки:**
  - розподіл вердиктів фактчеків;
  - ймовірності висновків;
  - сценарії з історією оцінок і графіком «що буде, якщо»;
  - календар прогнозів;
  - фонові збори;
  - звіт і журнал.

Темна та світла теми; працює й на телефоні.

### Інші команди

| Команда | Що робить |
|---|---|
| `social ask "питання"` | один запит без інтерфейсу; відповідь зберігається в `wiki/sessions/` |
| `social review` | червона команда: агент **іншого виробника** оскаржує висновки, фактчеки й сценарії; нічого не змінює, рецензія — у `wiki/reviews/` |
| `social config --agent codex --model …` | агент і модель за замовчуванням |
| `social config --role reviewer --agent agy …` | хто працює червоною командою |
| `social status` / `social jobs` | стан кейсу і фонові збори |

## Як це влаштовано

```
social chat ──► агент (Claude Code | Codex | Antigravity | opencode) + модель на вибір
                   │  читає AGENTS.md (методологія-рекомендації) і methodology/
                   └─► MCP-сервер social ──► тека кейсу (git)
                         case_status, precommit, collect_telegram/youtube/news, archive_url,
                         fact_check, judgment, forecast_define/observe/revise/resolve/state, report_build
```

- **Агент вирішує сам**, скільки збирати, які методи брати, які розділи писати. Методологія — це поради, а не рамки.
- **Тверді межі** лише ці:
  - тільки публічні дані;
  - не профілювати приватних осіб і не деанонімізувати власників каналів;
  - не змінювати сирі дані;
  - тримати ключі поза кейсом;
  - жодних платежів без дозволу.
- **Стан кейсу лежить у файлах:** `case.yaml`, `log.md`, `wiki/`, `data/`, `report/`. Тому продовжити кейс може інший агент або інша модель.
- **Попередня фіксація і сценарні моделі** комітяться в git кейсу до аналізу. Оновлення оцінок записуються в журнал з джерелом. Ймовірності перераховує код, а не «око».
- **Агенти працюють на вашій установці та вашому вході.** Social не бачить ваших облікових даних і не потребує API-ключів.

---

## English

Social lets you investigate information incidents with the coding agent and model of your choice (Claude Code, Codex, Antigravity, opencode). Install with `uv tool install "social-framework[youtube] @ git+https://github.com/cyberdid/social-framework"`, then `social new "Case"`, `cd` into it and `social chat --agent <agent> --model <model>`. The agent gets the methodology as recommendations (`AGENTS.md`) and case tools over MCP; `social review` runs a read-only red-team review by an agent from another vendor. The rest of this README describes the research code that ships alongside the app.

---

## Дослідницький код (src/)

Social is a modular Python research system for evidence-aware analysis of social conflicts and information incidents from digital traces. It combines platform-specific collection, versioned snapshots, text-similarity and timing analysis, an append-only evidence ledger, bounded retrieval and rule-based claim assessment, typed event organization, local media processing, and report quality gates.

This repository is the public code release that accompanies the preprint *Social: An Evidence-Grounded Framework for Automated Social Conflict and Information Incident Analysis*.

Preprint DOI (all versions): [10.5281/zenodo.24101594](https://doi.org/10.5281/zenodo.24101594).

## What this release contains

- `src/`: collectors, snapshot preparation, analysis modules, the evidence ledger, media pipeline and workflow runners.
- `schemas/`: JSON Schemas for the typed records.
- `tests/`: regression and contract tests that run without private data.

## What it does not contain

The research corpora, comments, media, reports, curated case builders and channel classifications are not included, because they contain personal data or case-specific material about identifiable people. Tests that need those artifacts are removed or skipped with a stated reason. Results reported in the preprint therefore cannot be fully replayed from this repository alone.

## Status and limits

Social is research software, not a validated autonomous investigator. Passing tests establish software behavior, not factual accuracy. Semantic accuracy of claim assessment has not been measured against independently adjudicated data. The relation rule is a deterministic heuristic and its confidence values are not calibrated probabilities. Statistical departures from a null model do not establish coordination, inauthenticity or attribution. Some modules still contain incident-specific keywords and paths from the original case and need adaptation before reuse.

Use public data only, respect platform terms and applicable law, and do not use this software to profile or identify private individuals.

## Running the tests

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-contracts.txt pytest numpy pandas scipy matplotlib telethon requests beautifulsoup4 datasketch networkx rapidfuzz pymorphy3 pymorphy3-dicts-uk pymorphy3-dicts-ru reportlab qrcode curl_cffi yt-dlp pyyaml trafilatura instaloader pillow av==18.1.0
pytest -q
```

## License

Source code is licensed under the Apache License 2.0. See `LICENSE`.
