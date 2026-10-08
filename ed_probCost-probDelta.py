"""
ed_probCost-probDelta.py
================
Monte Carlo del Economic Dispatch del delta diario
con incertidumbre en COSTOS y en el DELTA X.

Para cada simulación:
  1. Muestrea el delta X ~ Uniforme(X_min, X_max)   [opcional]
  2. Muestrea el costo de cada tecnología ~ Uniforme(Costo_min, Costo_max)
  3. Resuelve el ED determinístico con esos valores
  4. Guarda costo total, energía por tecnología, ENS y el X usado

Al final genera:
  - Resumen estadístico en consola
  - Archivo Excel con todas las realizaciones + percentiles
  - Gráficas en la carpeta plots/

Uso:
    python ed_probCost-probDelta.py

Dependencias:
    pip install pyomo pandas openpyxl numpy matplotlib scipy
    Solver CBC instalado y en el PATH
"""

import os
import sys
import time

import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import TerminationCondition, SolverStatus

import matplotlib
matplotlib.use("Agg")  # backend no interactivo (guarda archivos sin abrir ventana)
import matplotlib.pyplot as plt
from scipy import stats

# =============================================================================
# PARÁMETROS CONFIGURABLES
# =============================================================================
EXCEL_FILE = "data-input/costos_tecnologias.xlsx"
SHEET_NAME = 0
ENERGY_UNIT = "GWh"
CURRENCY = "COP"

# ----- Delta -----
# Si STOCHASTIC_DELTA = False → se usa DELTA_X fijo
# Si STOCHASTIC_DELTA = True  → se muestrea X ~ Uniforme(X_MIN, X_MAX)
STOCHASTIC_DELTA = True
DELTA_X = 300.0          # solo se usa si STOCHASTIC_DELTA = False
X_MIN = 200.0               # límite inferior del delta (GWh)
X_MAX = 400.0               # límite superior del delta (GWh)

# ----- Monte Carlo -----
N_SIM = 1000
RANDOM_SEED = 42

# ----- Modelo -----
ALLOW_ENS = True
ENS_PENALTY = 1.0e7          # COP/MWh
USE_MIN_TECH = True
MIN_TECH_BINARY = True      # False = LP puro (rápido)

SOLVER_NAME = "cbc"
SOLVER_TEE = False

# Archivos de salida
OUTPUT_EXCEL = "data-output/resultados_probCost-probDelta.xlsx"
PLOTS_DIR = "plots"

TO_MWH = {"MWH": 1.0, "GWH": 1000.0}
REQUIRED_COLS = ["Tecnologia", "Costo_min", "Costo_max"]


# =============================================================================
# LECTURA DE DATOS
# =============================================================================
def _find_column(df, prefix):
    for col in df.columns:
        if str(col).strip().lower().startswith(prefix.lower()):
            return col
    return None


def _unit_from_column(col_name):
    name = str(col_name).strip().upper()
    if name.endswith("GWH"):
        return "GWH"
    if name.endswith("MWH"):
        return "MWH"
    raise ValueError(f"Columna '{col_name}' debe terminar en _GWh o _MWh.")


def load_data(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No se encontró '{path}'.")

    df = pd.read_excel(path, sheet_name=SHEET_NAME, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    cap_col = _find_column(df, "Capacidad_max")
    if cap_col is None:
        missing.append("Capacidad_max_GWh (o _MWh)")
    if missing:
        raise ValueError(f"Faltan columnas: {missing}")

    min_col = _find_column(df, "Minimo_tecnico")
    target = ENERGY_UNIT.strip().upper()

    out = pd.DataFrame()
    out["Tecnologia"] = df["Tecnologia"].astype(str).str.strip()
    out["Costo_min"] = pd.to_numeric(df["Costo_min"], errors="coerce")
    out["Costo_max"] = pd.to_numeric(df["Costo_max"], errors="coerce")

    cap_factor = TO_MWH[_unit_from_column(cap_col)] / TO_MWH[target]
    out["Cap_max"] = pd.to_numeric(df[cap_col], errors="coerce") * cap_factor

    if min_col is not None and USE_MIN_TECH:
        min_factor = TO_MWH[_unit_from_column(min_col)] / TO_MWH[target]
        out["Min_tec"] = pd.to_numeric(df[min_col], errors="coerce").fillna(0.0) * min_factor
    else:
        out["Min_tec"] = 0.0

    out = out.dropna(subset=["Tecnologia", "Costo_min", "Costo_max", "Cap_max"]).reset_index(drop=True)
    if out.empty:
        raise ValueError("Excel sin filas válidas.")
    if (out["Costo_min"] > out["Costo_max"]).any():
        raise ValueError("Hay Costo_min > Costo_max.")
    return out


# =============================================================================
# MODELO PYOMO
# =============================================================================
def build_and_solve(df, delta_x, cost_mwh_dict, solver):
    """
    Construye y resuelve el ED con delta y costos dados.
    Retorna dict con: ok, costo_total, energia, ens
    """
    techs = df["Tecnologia"].tolist()
    cost_unit = {t: cost_mwh_dict[t] * TO_MWH[ENERGY_UNIT.upper()] for t in techs}
    cap = dict(zip(df["Tecnologia"], df["Cap_max"]))
    emin = dict(zip(df["Tecnologia"], df["Min_tec"]))
    ens_pen_unit = ENS_PENALTY * TO_MWH[ENERGY_UNIT.upper()]

    m = pyo.ConcreteModel()
    m.T = pyo.Set(initialize=techs, ordered=True)
    m.cost = pyo.Param(m.T, initialize=cost_unit)
    m.cap = pyo.Param(m.T, initialize=cap)
    m.emin = pyo.Param(m.T, initialize=emin)
    m.X = pyo.Param(initialize=delta_x)
    m.ens_pen = pyo.Param(initialize=ens_pen_unit)

    m.e = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.ens = pyo.Var(domain=pyo.NonNegativeReals)
    if not ALLOW_ENS:
        m.ens.fix(0.0)

    if USE_MIN_TECH and MIN_TECH_BINARY:
        m.u = pyo.Var(m.T, domain=pyo.Binary)
        m.lim_max = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] <= m.cap[t] * m.u[t])
        m.lim_min = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] >= m.emin[t] * m.u[t])
    else:
        m.lim_max = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] <= m.cap[t])
        if USE_MIN_TECH:
            m.lim_min = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] >= m.emin[t])

    m.balance = pyo.Constraint(expr=sum(m.e[t] for t in m.T) + m.ens == m.X)
    m.obj = pyo.Objective(
        expr=sum(m.cost[t] * m.e[t] for t in m.T) + m.ens_pen * m.ens,
        sense=pyo.minimize,
    )

    results = solver.solve(m, tee=SOLVER_TEE)
    term = results.solver.termination_condition
    status = results.solver.status

    if term not in (TerminationCondition.optimal, TerminationCondition.feasible) or status != SolverStatus.ok:
        return {
            "ok": False,
            "costo_total": np.nan,
            "ens": np.nan,
            "energia": {t: np.nan for t in techs},
        }

    energia = {t: pyo.value(m.e[t]) for t in techs}
    return {
        "ok": True,
        "costo_total": pyo.value(m.obj),
        "ens": pyo.value(m.ens),
        "energia": energia,
    }


# =============================================================================
# MONTE CARLO (costos + delta)
# =============================================================================
def run_montecarlo(df, n_sim=N_SIM, seed=RANDOM_SEED):
    techs = df["Tecnologia"].tolist()
    rng = np.random.default_rng(seed)

    solver = pyo.SolverFactory(SOLVER_NAME)
    if not solver.available(exception_flag=False):
        raise RuntimeError(f"Solver '{SOLVER_NAME}' no disponible.")

    records = []
    t0 = time.time()

    print(f"\nEjecutando {n_sim} simulaciones Monte Carlo...")
    if STOCHASTIC_DELTA:
        print(f"  Delta X ~ Uniforme({X_MIN:.1f}, {X_MAX:.1f}) {ENERGY_UNIT}")
    else:
        print(f"  Delta X fijo = {DELTA_X:.1f} {ENERGY_UNIT}")
    print(f"  Costos ~ Uniforme(min, max) por tecnología")
    print(f"  Capacidad total disponible = {df['Cap_max'].sum():.1f} {ENERGY_UNIT}")
    print("-" * 70)

    for s in range(n_sim):
        # 1. Muestrear delta
        if STOCHASTIC_DELTA:
            x_s = float(rng.uniform(X_MIN, X_MAX))
        else:
            x_s = DELTA_X

        # 2. Muestrear costos
        cost_mwh = {}
        for _, row in df.iterrows():
            t = row["Tecnologia"]
            cost_mwh[t] = float(rng.uniform(row["Costo_min"], row["Costo_max"]))

        # 3. Resolver
        sol = build_and_solve(df, x_s, cost_mwh, solver)

        # 4. Guardar
        rec = {
            "sim": s + 1,
            "ok": sol["ok"],
            "X": x_s,
            "costo_total": sol["costo_total"],
            "ENS": sol["ens"],
        }
        for t in techs:
            rec[f"E_{t}"] = sol["energia"][t]
            rec[f"C_{t}"] = cost_mwh[t]
        records.append(rec)

        if (s + 1) % 100 == 0 or (s + 1) == n_sim:
            elapsed = time.time() - t0
            print(f"  Simulación {s+1:4d}/{n_sim}  |  tiempo acumulado: {elapsed:.1f} s")

    return pd.DataFrame(records)


# =============================================================================
# RESUMEN Y GUARDADO
# =============================================================================
def summarize_and_save(results_df, techs):
    ok = results_df[results_df["ok"] == True].copy()
    n_ok = len(ok)
    n_total = len(results_df)

    print("\n" + "=" * 78)
    print(" RESUMEN MONTE CARLO - COSTOS + DELTA ESTOCÁSTICOS")
    print("=" * 78)
    print(f"Simulaciones totales     : {n_total}")
    print(f"Simulaciones exitosas    : {n_ok}")

    if STOCHASTIC_DELTA:
        print(f"Delta X               : Uniforme({X_MIN:.1f}, {X_MAX:.1f}) {ENERGY_UNIT}")
    else:
        print(f"Delta X               : {DELTA_X:.1f} {ENERGY_UNIT} (fijo)")

    if n_ok == 0:
        print("\n[!] Ninguna simulación produjo solución óptima.")
        return

    costo = ok["costo_total"]
    ens = ok["ENS"]
    x_vals = ok["X"]

    print(f"\n--- Delta X observado ({ENERGY_UNIT}) ---")
    print(f"  Media                  : {x_vals.mean():,.2f}")
    print(f"  Mínimo                 : {x_vals.min():,.2f}")
    print(f"  Máximo                 : {x_vals.max():,.2f}")

    print(f"\n--- Costo total ({CURRENCY}) ---")
    print(f"  Media                  : {costo.mean():,.2f}")
    print(f"  Desviación estándar    : {costo.std():,.2f}")
    print(f"  Mínimo                 : {costo.min():,.2f}")
    print(f"  Percentil 5 %          : {costo.quantile(0.05):,.2f}")
    print(f"  Mediana (P50)          : {costo.quantile(0.50):,.2f}")
    print(f"  Percentil 95 %         : {costo.quantile(0.95):,.2f}")
    print(f"  Máximo                 : {costo.max():,.2f}")

    print(f"\n--- Energía no suministrada (ENS) ---")
    print(f"  Media ENS              : {ens.mean():,.3f} {ENERGY_UNIT}")
    print(f"  Probabilidad ENS > 0   : {(ens > 1e-6).mean()*100:.1f} %")
    print(f"  Máximo ENS observado   : {ens.max():,.3f} {ENERGY_UNIT}")

    print(f"\n--- Energía media por tecnología ({ENERGY_UNIT}) ---")
    for t in techs:
        col = f"E_{t}"
        media = ok[col].mean()
        p5 = ok[col].quantile(0.05)
        p95 = ok[col].quantile(0.95)
        print(f"  {t:<20s}: media={media:8.2f}  |  P5={p5:8.2f}  |  P95={p95:8.2f}")

    # ------------------------------------------------------------------
    # Guardar Excel
    # ------------------------------------------------------------------
    with pd.ExcelWriter(OUTPUT_EXCEL, engine="openpyxl") as writer:
        # Hoja 1: todas las realizaciones
        ok.to_excel(writer, sheet_name="Realizaciones", index=False)

        # Hoja 2: resumen estadístico
        summary_rows = [
            ["Métrica", "Valor"],
            ["N simulaciones", n_total],
            ["N exitosas", n_ok],
            ["Delta estocástico", STOCHASTIC_DELTA],
            ["X_min", X_MIN if STOCHASTIC_DELTA else DELTA_X],
            ["X_max", X_MAX if STOCHASTIC_DELTA else DELTA_X],
            ["Unidad", ENERGY_UNIT],
            ["", ""],
            ["X - Media", x_vals.mean()],
            ["X - Min", x_vals.min()],
            ["X - Max", x_vals.max()],
            ["", ""],
            ["Costo total - Media", costo.mean()],
            ["Costo total - Std", costo.std()],
            ["Costo total - Min", costo.min()],
            ["Costo total - P5", costo.quantile(0.05)],
            ["Costo total - P50", costo.quantile(0.50)],
            ["Costo total - P95", costo.quantile(0.95)],
            ["Costo total - Max", costo.max()],
            ["", ""],
            ["ENS - Media", ens.mean()],
            ["ENS - Prob > 0 (%)", (ens > 1e-6).mean() * 100],
            ["ENS - Max", ens.max()],
        ]
        for t in techs:
            col = f"E_{t}"
            summary_rows.append([f"Energía media {t}", ok[col].mean()])
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Resumen", index=False, header=False)

        # Hoja 3: costos muestreados
        cost_cols = ["sim", "X"] + [f"C_{t}" for t in techs]
        ok[cost_cols].to_excel(writer, sheet_name="Costos_y_X_muestreados", index=False)

    print(f"\nResultados guardados en: {OUTPUT_EXCEL}")
    print("=" * 78)

    # Gráficas
    make_plots(ok, techs)


# =============================================================================
# GRÁFICAS
# =============================================================================
def make_plots(ok, techs):
    """
    Genera y guarda:
      1. Costos simulados de todas las tecnologías (una sola figura)
      2. Histograma + ajuste de distribución de energía por tecnología (una figura por tech)
      3. Histograma del Delta X simulado
    """
    os.makedirs(PLOTS_DIR, exist_ok=True)
    print(f"\nGenerando gráficas en '{PLOTS_DIR}/' ...")

    # ------------------------------------------------------------------
    # 1. Costos simulados – todas las tecnologías en una figura
    # ------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(10, 5))
    cost_data = [ok[f"C_{t}"].values for t in techs]
    # Compatibilidad: matplotlib >= 3.9 usa tick_labels; versiones anteriores usan labels
    try:
        bp = ax.boxplot(cost_data, tick_labels=techs, patch_artist=True, showmeans=True)
    except TypeError:
        bp = ax.boxplot(cost_data, labels=techs, patch_artist=True, showmeans=True)
    colors = plt.cm.Set2(np.linspace(0, 1, len(techs)))
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)
    ax.set_ylabel(f"Costo simulado ({CURRENCY}/MWh)")
    ax.set_title("Costos simulados por tecnología (Monte Carlo)")
    ax.tick_params(axis="x", rotation=30)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    path_costs = os.path.join(PLOTS_DIR, "costos_simulados_todas.png")
    fig.savefig(path_costs, dpi=150)
    plt.close(fig)
    print(f"  → {path_costs}")

    # ------------------------------------------------------------------
    # 2. Energía por tecnología – histograma + ajuste de distribución
    # ------------------------------------------------------------------
    for t in techs:
        col = f"E_{t}"
        data = ok[col].dropna().values
        if len(data) < 5:
            print(f"  [!] Poca data para {t}, se omite gráfica de energía.")
            continue

        fig, ax = plt.subplots(figsize=(8, 4.5))

        # Histograma normalizado
        n_bins = min(40, max(10, int(np.sqrt(len(data)))))
        ax.hist(data, bins=n_bins, density=True, alpha=0.55, color="steelblue",
                edgecolor="white", label="Histograma")

        # Ajuste: probar normal y, si hay valores en el borde (capacidad),
        # también mostrar KDE no paramétrico
        try:
            mu, sigma = stats.norm.fit(data)
            x_grid = np.linspace(data.min(), data.max(), 200)
            if sigma > 1e-9:
                pdf_norm = stats.norm.pdf(x_grid, mu, sigma)
                ax.plot(x_grid, pdf_norm, "r-", lw=2,
                        label=f"Normal(μ={mu:.1f}, σ={sigma:.1f})")
        except Exception:
            pass

        # KDE (suavizado no paramétrico)
        try:
            if data.std() > 1e-9:
                kde = stats.gaussian_kde(data)
                x_grid = np.linspace(data.min(), data.max(), 200)
                ax.plot(x_grid, kde(x_grid), "g--", lw=1.8, label="KDE")
        except Exception:
            pass

        ax.axvline(data.mean(), color="black", ls=":", lw=1.2,
                   label=f"Media = {data.mean():.2f}")
        ax.set_xlabel(f"Energía ({ENERGY_UNIT})")
        ax.set_ylabel("Densidad")
        ax.set_title(f"Energía utilizada – {t}")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
        fig.tight_layout()

        safe_name = t.replace(" ", "_").replace("/", "-")
        path_e = os.path.join(PLOTS_DIR, f"energia_{safe_name}.png")
        fig.savefig(path_e, dpi=150)
        plt.close(fig)
        print(f"  → {path_e}")

    # ------------------------------------------------------------------
    # 3. Delta X simulado
    # ------------------------------------------------------------------
    x_vals = ok["X"].dropna().values
    if len(x_vals) >= 5:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        n_bins = min(40, max(10, int(np.sqrt(len(x_vals)))))
        ax.hist(x_vals, bins=n_bins, density=True, alpha=0.55, color="darkorange",
                edgecolor="white", label="Histograma")

        # Ajuste uniforme teórico (si se usó uniforme)
        if STOCHASTIC_DELTA and X_MAX > X_MIN:
            height = 1.0 / (X_MAX - X_MIN)
            ax.hlines(height, X_MIN, X_MAX, colors="red", lw=2,
                      label=f"Uniforme({X_MIN:.0f}, {X_MAX:.0f})")

        # KDE
        try:
            if x_vals.std() > 1e-9:
                kde = stats.gaussian_kde(x_vals)
                x_grid = np.linspace(x_vals.min(), x_vals.max(), 200)
                ax.plot(x_grid, kde(x_grid), "g--", lw=1.8, label="KDE")
        except Exception:
            pass

        ax.axvline(x_vals.mean(), color="black", ls=":", lw=1.2,
                   label=f"Media = {x_vals.mean():.2f}")
        ax.set_xlabel(f"Delta X ({ENERGY_UNIT})")
        ax.set_ylabel("Densidad")
        ax.set_title("Delta X simulado (residual no hidráulico)")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        path_x = os.path.join(PLOTS_DIR, "delta_X_simulado.png")
        fig.savefig(path_x, dpi=150)
        plt.close(fig)
        print(f"  → {path_x}")

    print("Gráficas generadas.")


# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        df = load_data(EXCEL_FILE)
        techs = df["Tecnologia"].tolist()
        print(f"Tecnologías leídas: {len(df)}")
        print(df[["Tecnologia", "Costo_min", "Costo_max", "Cap_max", "Min_tec"]].to_string(index=False))

        if STOCHASTIC_DELTA and X_MIN >= X_MAX:
            raise ValueError("X_MIN debe ser estrictamente menor que X_MAX.")

        results = run_montecarlo(df, n_sim=N_SIM, seed=RANDOM_SEED)
        summarize_and_save(results, techs)

    except (FileNotFoundError, ValueError, RuntimeError) as err:
        print(f"\n[ERROR] {err}", file=sys.stderr)
        sys.exit(1)
    except Exception as err:
        print(f"\n[ERROR inesperado] {type(err).__name__}: {err}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
