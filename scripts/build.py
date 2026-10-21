#!/usr/bin/env python3
"""
build.py -- compila i documenti LaTeX e prepara il sito con i PDF.

Si lancia dalla cartella principale del repository:

    python scripts/build.py                  compila tutti i documenti e crea site/
    python scripts/build.py FILE.tex [...]   compila solo i file indicati
    python scripts/build.py --esempi         compila anche modelli ed esempi
    python scripts/build.py --list           elenca i documenti trovati
    python scripts/build.py --clean          cancella build/ e site/, poi compila

Nelle pull request la CI usa:

    python scripts/build.py --anteprima origin/main
        compila TUTTO (documenti, modelli ed esempi), così nessun errore
        sfugge, e prepara il commento con i soli documenti toccati.

Dove finiscono i file:
    build/   file temporanei di LaTeX (si possono cancellare quando volete)
    site/    i PDF con la versione nel nome e index.html: è il sito
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
SITE = ROOT / "site"
ANTEPRIMA = BUILD / "anteprima"
LATEXMKRC = ROOT / "latexmkrc"

# Cartelle (di primo livello) in cui NON ci sono documenti da pubblicare
CARTELLE_ESCLUSE = {"template", "scripts", "build", "site", "allegati",
                    "node_modules"}

# Modelli ed esempi: si compilano nelle pull request, per controllare che
# la classe funzioni ancora, ma non finiscono sul sito
CARTELLE_ESEMPI = ("template/modelli", "template/esempi")

# Sezioni del sito, nell'ordine in cui compaiono
SEZIONI = [
    ("candidatura", "Candidatura"),
    ("lettere", "Lettere di presentazione"),
    ("documenti", "Documenti"),
    ("verbali/esterni", "Verbali esterni"),
    ("verbali/interni", "Verbali interni"),
]

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]


@dataclass
class Documento:
    sorgente: Path                  # percorso assoluto del .tex
    esempio: bool = False           # modello/esempio: si compila ma non si pubblica
    titolo: str = ""
    versione: str = ""              # dalla PRIMA riga \modifica del registro
    data: str = ""                  # AAAA-MM-GG
    tipo: str = ""                  # verbale interno/esterno, documento, lettera
    ok: bool = False                # compilato senza errori?
    errori: list[str] = field(default_factory=list)
    dipendenze: set[str] = field(default_factory=set)  # file letti da LaTeX

    @property
    def relativo(self) -> Path:
        return self.sorgente.relative_to(ROOT)

    @property
    def sezione(self) -> str:
        rel = self.relativo.as_posix()
        for prefisso, _ in SEZIONI:
            if rel.startswith(prefisso + "/"):
                return prefisso
        return "altro"

    @property
    def nome_pdf(self) -> str:
        suffisso = f"_v{self.versione}" if self.versione else ""
        return f"{self.sorgente.stem}{suffisso}.pdf"

    @property
    def pdf_build(self) -> Path:
        return BUILD / self.relativo.parent / f"{self.sorgente.stem}.pdf"

    @property
    def pdf_sito(self) -> Path:
        return SITE / self.relativo.parent / self.nome_pdf


# --------------------------------------------------------------------------
#  Lettura dei dati dal sorgente .tex
# --------------------------------------------------------------------------
COMMENTO = re.compile(r"(?<!\\)%.*")


def senza_commenti(testo: str) -> str:
    """Toglie i commenti LaTeX (da % a fine riga, ma non \\%)."""
    return "\n".join(COMMENTO.sub("", riga) for riga in testo.splitlines())


def argomenti(testo: str, comando: str, quanti: int = 1) -> list[str]:
    """Primi `quanti` argomenti {...} della PRIMA occorrenza di \\comando."""
    m = re.search(r"\\" + comando + r"(?![A-Za-z@])", testo)
    if not m:
        return []
    i, risultato = m.end(), []
    while len(risultato) < quanti:
        while i < len(testo) and testo[i] in " \t\r\n":
            i += 1
        if i >= len(testo) or testo[i] != "{":
            break
        livello, inizio = 1, i + 1
        i += 1
        while i < len(testo) and livello:
            if testo[i] == "\\":        # salta \{ e \}
                i += 2
                continue
            if testo[i] == "{":
                livello += 1
            elif testo[i] == "}":
                livello -= 1
            i += 1
        risultato.append(testo[inizio:i - 1].strip())
    return risultato


def pulisci(latex: str) -> str:
    """Rende leggibile un titolo LaTeX per la pagina web."""
    t = latex.replace("~", " ").replace("--", "–").replace("\\&", "&")
    t = re.sub(r"\\[a-zA-Z]+\*?\s*", "", t)
    return re.sub(r"[{}]", "", t).strip()


def tipo_documento(testo: str) -> str:
    m = re.search(r"\\documentclass\s*\[([^\]]*)\]\s*\{gpt\}", testo)
    opzioni = [o.replace(" ", "") for o in m.group(1).split(",")] if m else []
    if "documento" in opzioni:
        return "documento"
    if "lettera" in opzioni:
        return "lettera"
    if "verbale=esterno" in opzioni or "esterno" in opzioni:
        return "verbale esterno"
    return "verbale interno"


def leggi(sorgente: Path, esempio: bool = False) -> Documento:
    testo = senza_commenti(sorgente.read_text(encoding="utf-8"))
    d = Documento(sorgente, esempio=esempio)
    titolo = argomenti(testo, "titolo")
    d.titolo = (pulisci(titolo[0]) if titolo else "") or sorgente.stem
    # versione e data dell'ultima modifica: la PRIMA riga del registro
    # (la classe gpt.cls fa esattamente la stessa cosa)
    modifica = argomenti(testo, "modifica", 2)
    d.versione = modifica[0] if modifica else ""
    data = argomenti(testo, "data")
    d.data = data[0] if data else (modifica[1] if len(modifica) > 1 else "")
    d.tipo = tipo_documento(testo)
    return d


def ha_documentclass(percorso: Path) -> bool:
    return "\\documentclass" in senza_commenti(percorso.read_text(encoding="utf-8"))


def trova_documenti() -> list[Path]:
    """I documenti da pubblicare: tutti i .tex con \\documentclass fuori
    dalle cartelle escluse (i capitoli inclusi con \\input non ce l'hanno)."""
    candidati: list[Path] = []
    for voce in sorted(ROOT.iterdir()):
        if voce.is_dir():
            if voce.name in CARTELLE_ESCLUSE or voce.name.startswith("."):
                continue
            candidati.extend(sorted(voce.rglob("*.tex")))
        elif voce.suffix == ".tex":
            candidati.append(voce)
    return [p for p in candidati if ha_documentclass(p)]


def trova_esempi() -> list[Path]:
    trovati: list[Path] = []
    for cartella in CARTELLE_ESEMPI:
        if (ROOT / cartella).is_dir():
            trovati.extend(p for p in sorted((ROOT / cartella).rglob("*.tex"))
                           if ha_documentclass(p))
    return trovati


# --------------------------------------------------------------------------
#  Compilazione
# --------------------------------------------------------------------------
RIGA_ERRORE = re.compile(
    r"^(?P<file>.+?\.(?:tex|cls|sty|ldf|def|cfg|clo)):(?P<riga>\d+): (?P<msg>.*)$")


def percorso_leggibile(percorso: str, d: Documento) -> str:
    """Trasforma un percorso del log in uno relativo al repository."""
    p = Path(percorso)
    if not p.is_absolute():
        p = d.sorgente.parent / p
    try:
        return Path(os.path.normpath(p)).relative_to(ROOT).as_posix()
    except ValueError:
        return percorso


def estrai_errori(d: Documento, log: Path, uscita: str) -> list[str]:
    """Le righe utili del log: "file:riga: messaggio" e il testo incriminato."""
    righe = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    errori: list[str] = []
    for n, riga in enumerate(righe):
        # "ignored error": avviso innocuo di TeX Live 2025+ quando una tabella
        # lunga va a capo pagina; "Fatal error": ripete l'errore vero
        if "==> Fatal error occurred" in riga or "ignored error" in riga:
            continue
        m = RIGA_ERRORE.match(riga)
        if m:
            testo = f"{percorso_leggibile(m['file'], d)}:{m['riga']}: {m['msg']}"
        elif riga.startswith("! "):
            testo = riga
        else:
            continue
        if testo in errori:
            continue
        errori.append(testo)
        # la riga "l.12 ..." subito dopo mostra il punto esatto del testo
        for seguito in righe[n + 1:n + 8]:
            if re.match(r"^l\.\d+ ", seguito):
                errori.append("    " + seguito)
                break
    if not errori:  # niente nel log: mostro la fine dell'output di latexmk
        errori = [r for r in uscita.splitlines()
                  if r.strip() and "extra time" not in r and "not process it" not in r][-15:]
    return errori[:10]


def leggi_dipendenze(fls: Path) -> set[str]:
    """File del repository letti da LaTeX (dal file .fls di latexmk)."""
    trovati: set[str] = set()
    if not fls.exists():
        return trovati
    cartella = fls.parent
    for riga in fls.read_text(encoding="utf-8", errors="replace").splitlines():
        if riga.startswith("PWD "):
            cartella = Path(riga[4:])
        elif riga.startswith("INPUT "):
            p = Path(riga[6:])
            if not p.is_absolute():
                p = cartella / p
            try:
                rel = Path(os.path.realpath(p)).relative_to(ROOT)
            except ValueError:
                continue  # file di sistema (TeX Live)
            if rel.parts and rel.parts[0] not in ("build", "site"):
                trovati.add(rel.as_posix())
    return trovati


def compila(d: Documento) -> bool:
    outdir = BUILD / d.relativo.parent
    outdir.mkdir(parents=True, exist_ok=True)
    # latexmk parte dalla cartella del documento, esattamente come fa VS Code:
    # così \input{capitoli/...} e le immagini funzionano uguali dappertutto.
    comando = ["latexmk", "-r", str(LATEXMKRC), f"-outdir={outdir}", d.sorgente.name]
    ambiente = dict(os.environ, max_print_line="10000")  # errori su una riga sola
    esito = subprocess.run(comando, cwd=d.sorgente.parent, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=ambiente)
    d.dipendenze = leggi_dipendenze(outdir / f"{d.sorgente.stem}.fls")
    d.ok = esito.returncode == 0 and d.pdf_build.exists()
    if not d.ok:
        d.errori = estrai_errori(d, outdir / f"{d.sorgente.stem}.log",
                                 esito.stdout + esito.stderr)
    return d.ok


def file_cambiati(base: str) -> set[str]:
    """File cambiati rispetto a `base` (-z: nomi con accenti senza sorprese)."""
    esito = subprocess.run(["git", "diff", "--name-only", "-z", f"{base}...HEAD"],
                           cwd=ROOT, capture_output=True)
    if esito.returncode != 0:
        motivo = esito.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"Errore: non riesco a confrontare con '{base}': {motivo}\n"
                         "Controllate che il ramo esista (per esempio con: git fetch origin).")
    return {p for p in esito.stdout.decode("utf-8", "replace").split("\0") if p}


# --------------------------------------------------------------------------
#  Sito
# --------------------------------------------------------------------------
def data_leggibile(iso: str) -> str:
    try:
        a, m, g = (int(x) for x in iso.split("-"))
        return f"{g} {MESI[m - 1]} {a}"
    except (ValueError, IndexError):
        return iso


def pdf_dello_stesso_documento(d: Documento) -> list[Path]:
    """Tutte le versioni di questo documento già presenti in site/."""
    cartella = d.pdf_sito.parent
    if not cartella.is_dir():
        return []
    schema = re.compile(re.escape(d.sorgente.stem) + r"(_v[0-9.]+)?\.pdf")
    return [p for p in cartella.iterdir() if schema.fullmatch(p.name)]


def copia_nel_sito(d: Documento) -> None:
    for vecchio in pdf_dello_stesso_documento(d):   # niente versioni vecchie
        vecchio.unlink()
    d.pdf_sito.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(d.pdf_build, d.pdf_sito)


def togli_dal_sito(d: Documento) -> None:
    """Se un documento non compila, il sito locale non mostra un PDF vecchio."""
    for vecchio in pdf_dello_stesso_documento(d):
        vecchio.unlink()


def scrivi_sito(documenti: list[Documento]) -> None:
    SITE.mkdir(parents=True, exist_ok=True)
    # logo nero (sfondo chiaro) e logo bianco (sfondo scuro): i PDF usano
    # sempre quello nero, il sito sceglie in base al tema del visitatore
    for nome in ("logo.png", "logo-bianco.png"):
        logo = ROOT / "template" / "img" / nome
        if logo.exists():
            shutil.copy2(logo, SITE / nome)

    elenco = [{
        "titolo": d.titolo,
        "versione": d.versione,
        "data": d.data,
        "tipo": d.tipo,
        "sezione": d.sezione,
        "sorgente": d.relativo.as_posix(),
        "pdf": d.pdf_sito.relative_to(SITE).as_posix(),
    } for d in documenti if d.pdf_sito.exists()]
    (SITE / "documenti.json").write_text(
        json.dumps(elenco, ensure_ascii=False, indent=2), encoding="utf-8")

    blocchi = []
    for chiave, nome in SEZIONI + [("altro", "Altro")]:
        voci = [d for d in documenti if d.sezione == chiave and d.pdf_sito.exists()]
        if not voci:
            continue
        # verbali dal più recente; gli altri documenti in ordine alfabetico
        if chiave.startswith("verbali"):
            voci.sort(key=lambda d: d.data, reverse=True)
        else:
            voci.sort(key=lambda d: d.titolo.lower())
        righe = []
        for d in voci:
            href = html.escape(d.pdf_sito.relative_to(SITE).as_posix())
            data = f'<span class="data">{html.escape(data_leggibile(d.data))}</span>' if d.data else ""
            ver = f'<span class="ver">v{html.escape(d.versione)}</span>' if d.versione else ""
            righe.append(
                f'<li><a href="{href}">{html.escape(d.titolo)}</a>'
                f'<span class="meta">{data}{ver}</span></li>')
        blocchi.append(f"<section><h2>{html.escape(nome)}</h2><ul>{''.join(righe)}</ul></section>")

    aggiornato = data_leggibile(date.today().isoformat())
    pagina = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Git Push Team · Documentazione</title>
<link rel="icon" href="logo.png" media="(prefers-color-scheme: light)">
<link rel="icon" href="logo-bianco.png" media="(prefers-color-scheme: dark)">
<style>
:root {{
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
  --accent: #14457a; --tint: #f3f6fa;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #111418; --fg: #e8eaed; --muted: #9aa3ad; --line: #2a2f36;
           --accent: #7fb0e6; --tint: #171c22; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg);
       font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 48px 16px 64px; }}
header {{ display: flex; align-items: center; gap: 20px; margin-bottom: 40px; }}
.logo {{ width: 88px; height: 88px; object-fit: contain; }}
h1 {{ font-size: 28px; margin: 0; }}
.sub {{ color: var(--muted); margin: 4px 0 0; }}
h2 {{ font-size: 13px; letter-spacing: .08em; text-transform: uppercase;
      color: var(--accent); margin: 36px 0 8px; }}
ul {{ list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }}
li {{ display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
      padding: 12px 4px; border-bottom: 1px solid var(--line); }}
li:hover {{ background: var(--tint); }}
a {{ color: var(--fg); text-decoration: none; font-weight: 500; }}
a:hover {{ color: var(--accent); text-decoration: underline; }}
.meta {{ color: var(--muted); font-size: 14px; display: flex; gap: 12px; }}
.ver {{ font-variant-numeric: tabular-nums; }}
footer {{ margin-top: 48px; color: var(--muted); font-size: 13px; }}
</style>
</head>
<body>
<main>
<header>
  <picture>
    <source srcset="logo-bianco.png" media="(prefers-color-scheme: dark)">
    <img class="logo" src="logo.png" alt="Logo del Git Push Team">
  </picture>
  <div>
    <h1>Git Push Team</h1>
    <p class="sub">Gruppo 9 · Ingegneria del Software · Università degli Studi di Padova</p>
  </div>
</header>
{''.join(blocchi) or '<p>Nessun documento ancora pubblicato.</p>'}
<footer>Aggiornato il {aggiornato}. Contatti: <a href="mailto:gitpushteam@gmail.com">gitpushteam@gmail.com</a></footer>
</main>
</body>
</html>
"""
    (SITE / "index.html").write_text(pagina, encoding="utf-8")


# --------------------------------------------------------------------------
#  Riepiloghi (terminale, commento nella PR, pagina dell'Action)
# --------------------------------------------------------------------------
def riga_tabella(d: Documento) -> str:
    nota = " _(modello/esempio)_" if d.esempio else ""
    esito = "✅ compilato" if d.ok else "❌ errore"
    return f"| `{d.relativo.as_posix()}`{nota} | {d.versione or '—'} | {esito} |"


def blocchi_errori(falliti: list[Documento]) -> list[str]:
    righe: list[str] = []
    for d in falliti:
        righe += ["", f"**Errori in `{d.relativo.as_posix()}`:**", "```", *d.errori, "```"]
    return righe


def riepilogo_semplice(documenti: list[Documento]) -> str:
    righe = ["| Documento | Versione | Esito |", "| --- | --- | --- |"]
    righe += [riga_tabella(d) for d in documenti]
    righe += blocchi_errori([d for d in documenti if not d.ok])
    return "\n".join(righe)


def riepilogo_anteprima(toccati: list[Documento], falliti: list[Documento],
                        n_documenti: int, n_esempi: int) -> str:
    righe: list[str] = []
    if toccati:
        tabella = ["| Documento | Versione | Esito |", "| --- | --- | --- |"]
        tabella += [riga_tabella(d) for d in toccati]
        if len(toccati) > 8:   # elenco lungo (es. modifica alla classe): richiudibile
            righe += [f"<details><summary><b>{len(toccati)} documenti toccati da questa "
                      "pull request</b> (cliccate per vedere l'elenco)</summary>", "",
                      *tabella, "", "</details>"]
        else:
            righe += ["**Documenti toccati da questa pull request:**", "", *tabella]
    else:
        righe.append("Questa pull request non modifica nessun documento.")
    toccati_rel = {d.relativo for d in toccati}
    altri = [d for d in falliti if d.relativo not in toccati_rel]
    if altri:
        righe += ["", "⚠️ **Documenti NON toccati da questa pull request che però non compilano:**"]
        righe += [f"- `{d.relativo.as_posix()}`" for d in altri]
    righe += blocchi_errori(falliti)
    esito = "nessun errore ✅" if not falliti else f"{len(falliti)} con errori ❌"
    righe += ["", f"Controllo completo: compilati {n_documenti} documenti "
                  f"e {n_esempi} modelli/esempi, {esito}."]
    return "\n".join(righe)


def salva_riepilogo(testo: str) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "riepilogo.md").write_text(testo + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(testo + "\n")


def scrivi_output_github(**valori: str) -> None:
    """Valori letti dai passi successivi del workflow (steps.build.outputs.*)."""
    percorso = os.environ.get("GITHUB_OUTPUT")
    if percorso:
        with open(percorso, "a", encoding="utf-8") as f:
            for chiave, valore in valori.items():
                f.write(f"{chiave}={valore}\n")


# --------------------------------------------------------------------------
#  Modalità anteprima (pull request)
# --------------------------------------------------------------------------
def anteprima(base: str) -> int:
    cambiati = file_cambiati(base)
    documenti = [leggi(p) for p in trova_documenti()]
    esempi = [leggi(p, esempio=True) for p in trova_esempi()]
    tutti = documenti + esempi
    for d in tutti:
        print(f"Compilo {d.relativo.as_posix()} ...", flush=True)
        compila(d)

    # toccato = il suo .tex è cambiato, oppure è cambiato un file che LaTeX
    # ha letto per compilarlo (capitoli, immagini, classe, dati del gruppo)
    toccati = [d for d in tutti
               if d.relativo.as_posix() in cambiati or d.dipendenze & cambiati]
    falliti = [d for d in tutti if not d.ok]

    shutil.rmtree(ANTEPRIMA, ignore_errors=True)
    pdf: list[Path] = []
    for d in toccati:
        if d.ok:
            destinazione = ANTEPRIMA / d.relativo.parent / d.nome_pdf
            destinazione.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d.pdf_build, destinazione)
            pdf.append(destinazione)
    scrivi_output_github(
        quanti=str(len(pdf)),
        pdf=pdf[0].relative_to(ROOT).as_posix() if len(pdf) == 1 else "")

    testo = riepilogo_anteprima(toccati, falliti, len(documenti), len(esempi))
    salva_riepilogo(testo)
    print()
    print(testo)
    return 1 if falliti else 0


# --------------------------------------------------------------------------
def main() -> int:
    for flusso in (sys.stdout, sys.stderr):   # emoji anche su console non UTF-8
        if hasattr(flusso, "reconfigure"):
            flusso.reconfigure(errors="replace")

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", nargs="*", help="documenti da compilare (predefinito: tutti)")
    p.add_argument("--esempi", action="store_true",
                   help="compila anche i modelli e gli esempi di template/")
    p.add_argument("--anteprima", metavar="BASE",
                   help="modalità pull request: compila tutto e riassume i documenti "
                        "toccati rispetto a BASE (es. origin/main)")
    p.add_argument("--list", action="store_true", help="elenca i documenti e termina")
    p.add_argument("--no-site", action="store_true", help="non generare il sito")
    p.add_argument("--clean", action="store_true", help="cancella build/ e site/ prima di iniziare")
    args = p.parse_args()

    if shutil.which("latexmk") is None and not args.list:
        print("Errore: latexmk non trovato. Installate TeX Live (vedi GUIDA.md).",
              file=sys.stderr)
        return 2

    if args.clean:
        shutil.rmtree(BUILD, ignore_errors=True)
        shutil.rmtree(SITE, ignore_errors=True)

    if args.anteprima:
        return anteprima(args.anteprima)

    pubblicabili = trova_documenti()
    if args.file:
        scelti = []
        for f in args.file:
            percorso = Path(f).resolve()
            if not percorso.is_file() or ROOT not in percorso.parents:
                print(f"Errore: file non trovato nel repository: {f}", file=sys.stderr)
                return 2
            scelti.append(percorso)
    else:
        scelti = pubblicabili + (trova_esempi() if args.esempi else [])

    da_pubblicare = set(pubblicabili)
    documenti = [leggi(s, esempio=s not in da_pubblicare) for s in scelti]

    if args.list:
        for d in documenti:
            nota = "  (modello/esempio)" if d.esempio else ""
            versione = f"v{d.versione}" if d.versione else "—"
            print(f"{d.relativo.as_posix():58} {versione:10} {d.titolo}{nota}")
        return 0

    if not documenti:
        print("Nessun documento da compilare.")
        return 0

    for d in documenti:
        print(f"Compilo {d.relativo.as_posix()} ...", flush=True)
        compila(d)
        if d.esempio:
            continue
        if d.ok:
            copia_nel_sito(d)
        else:
            togli_dal_sito(d)

    if not args.no_site:
        # l'indice elenca tutto ciò che c'è in site/, non solo questa esecuzione
        scrivi_sito([leggi(s) for s in pubblicabili])

    testo = riepilogo_semplice(documenti)
    salva_riepilogo(testo)
    print()
    print(testo)
    falliti = [d for d in documenti if not d.ok]
    if falliti:
        print(f"\n{len(falliti)} documento/i con errori: leggete le righe qui sopra.",
              file=sys.stderr)
        return 1
    pubblicati = sum(1 for d in documenti if not d.esempio)
    print(f"\nFatto: {pubblicati} PDF in {SITE.relative_to(ROOT)}/ "
          f"(i file temporanei sono in {BUILD.relative_to(ROOT)}/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
