"""
ed_deterministic.py
==============
Economic Dispatch simple (asignación de energía diaria) para cubrir el
residual de energía no hidráulica de un día completo.

Modelo (LP):
    min   sum_i c_i * e_i  +  C_ens * ens
    s.t.  sum_i e_i + ens = X                 (balance de energía del día)
          Emin_i <= e_i <= Emax_i             (límites por tecnología)
          ens >= 0                            (energía no suministrada)

Uso:
    python ed_residual.py

Dependencias:
    pip install pyomo pandas openpyxl
    Solver CBC instalado y en el PATH (conda install -c conda-forge coincbc,
    o apt install coinor-cbc, o descargar binarios de COIN-OR).
"""

import os
import sys

import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import TerminationCondition, SolverStatus

# =============================================================================
# PARÁMETROS CONFIGURABLES
# =============================================================================


RESIDUAL_X = 350.0                       # Residual de energía del día (en ENERGY_UNIT)
EXCEL_FILE = "data-input/costos_tecnologias.xlsx"   # Archivo Excel con los datos
SHEET_NAME = 0                           # Hoja a leer (índice o nombre)
ENERGY_UNIT = "GWh"                      # "GWh" o "MWh" (unidad de X y de los resultados)
CURRENCY = "COP"                         # Etiqueta de moneda (COP, USD, ...) -> costos por MWh en el Excel

# Energía no suministrada (ENS)
ALLOW_ENS = True                         # True: balance con ENS penalizada; False: balance exacto
ENS_PENALTY = 1.0e7                      # Penalización (moneda / MWh), debe ser >> costos reales

# Costos
COST_MODE = "mean"                       # "mean" (promedio de min y max) o "uniform" (muestreo)
RANDOM_SEED = 42                         # Semilla si COST_MODE == "uniform"

# Mínimo técnico
USE_MIN_TECH = True                      # Considerar la columna Minimo_tecnico (si existe)
MIN_TECH_BINARY = False                  # False: mínimo = cota inferior forzada (LP puro)
                                         # True : la tecnología opera en 0 o en [min, max] (MILP)

# Solver
SOLVER_NAME = "cbc"                      # "cbc" (defecto), "gurobi", "cplex", "glpk", "highs"...
SOLVER_EXECUTABLE = None                 # Ruta al ejecutable si no está en el PATH (opcional)
SOLVER_TEE = False                       # True para ver el log del solver

# Factores de conversión de energía a MWh
TO_MWH = {"MWH": 1.0, "GWH": 1000.0}

REQUIRED_COLS = ["Tecnologia", "Costo_min", "Costo_max"]


# =============================================================================
# LECTURA Y VALIDACIÓN DE DATOS
# =============================================================================
def _find_column(df, prefix):
    """Devuelve el nombre de la primera columna que empiece por `prefix` (sin distinguir mayúsculas)."""
    for col in df.columns:
        if str(col).strip().lower().startswith(prefix.lower()):
            return col
    return None


def _unit_from_column(col_name):
    """Detecta la unidad (GWH/MWH) a partir del sufijo del nombre de la columna."""
    name = str(col_name).strip().upper()
    if name.endswith("GWH"):
        return "GWH"
    if name.endswith("MWH"):
        return "MWH"
    raise ValueError(
        f"No se pudo detectar la unidad de la columna '{col_name}'. "
        f"Debe terminar en '_GWh' o '_MWh'."
    )


def load_data(path):
    """
    Lee el Excel y devuelve un DataFrame limpio con:
        Tecnologia, Costo_min, Costo_max, Cap_max, Min_tec   (energías en ENERGY_UNIT)
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No se encontró el archivo '{path}'. "
            f"Verifique el nombre/ruta o cree el Excel con el formato indicado al final de este script."
        )

    try:
        df = pd.read_excel(path, sheet_name=SHEET_NAME, engine="openpyxl")
    except Exception as exc:  # archivo corrupto, hoja inexistente, etc.
        raise RuntimeError(f"No se pudo leer el Excel '{path}': {exc}") from exc

    df.columns = [str(c).strip() for c in df.columns]

    # --- Columnas obligatorias -------------------------------------------------
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    cap_col = _find_column(df, "Capacidad_max")
    if cap_col is None:
        missing.append("Capacidad_max_GWh (o Capacidad_max_MWh)")
    if missing:
        raise ValueError(
            f"Faltan columnas obligatorias en el Excel: {missing}. "
            f"Columnas encontradas: {list(df.columns)}"
        )

    min_col = _find_column(df, "Minimo_tecnico")

    out = pd.DataFrame()
    out["Tecnologia"] = df["Tecnologia"].astype(str).str.strip()
    out["Costo_min"] = pd.to_numeric(df["Costo_min"], errors="coerce")
    out["Costo_max"] = pd.to_numeric(df["Costo_max"], errors="coerce")

    # --- Conversión de unidades de energía al ENERGY_UNIT elegido -------------
    target = ENERGY_UNIT.strip().upper()
    if target not in TO_MWH:
        raise ValueError(f"ENERGY_UNIT debe ser 'GWh' o 'MWh'. Recibido: '{ENERGY_UNIT}'")

    cap_factor = TO_MWH[_unit_from_column(cap_col)] / TO_MWH[target]
    out["Cap_max"] = pd.to_numeric(df[cap_col], errors="coerce") * cap_factor

    if min_col is not None and USE_MIN_TECH:
        min_factor = TO_MWH[_unit_from_column(min_col)] / TO_MWH[target]
        out["Min_tec"] = pd.to_numeric(df[min_col], errors="coerce").fillna(0.0) * min_factor
    else:
        out["Min_tec"] = 0.0

    # --- Limpieza y validaciones ----------------------------------------------
    out = out.dropna(subset=["Tecnologia", "Costo_min", "Costo_max", "Cap_max"]).reset_index(drop=True)
    if out.empty:
        raise ValueError("El Excel no contiene filas válidas (revise valores vacíos o no numéricos).")
    if out["Tecnologia"].duplicated().any():
        dup = out.loc[out["Tecnologia"].duplicated(), "Tecnologia"].tolist()
        raise ValueError(f"Tecnologías duplicadas en el Excel: {dup}")
    if (out["Costo_min"] > out["Costo_max"]).any():
        raise ValueError("Hay filas con Costo_min > Costo_max.")
    if (out["Cap_max"] < 0).any() or (out["Min_tec"] < 0).any():
        raise ValueError("Capacidades y mínimos técnicos no pueden ser negativos.")
    if (out["Min_tec"] > out["Cap_max"]).any():
        bad = out.loc[out["Min_tec"] > out["Cap_max"], "Tecnologia"].tolist()
        raise ValueError(f"Mínimo técnico mayor que la capacidad máxima en: {bad}")

    return out


def compute_costs(df):
    """
    Calcula el costo por MWh de cada tecnología según COST_MODE y lo convierte
    a costo por ENERGY_UNIT (necesario para que el modelo sea consistente).
    """
    if COST_MODE.lower() == "mean":
        cost_mwh = (df["Costo_min"] + df["Costo_max"]) / 2.0
    elif COST_MODE.lower() == "uniform":
        rng = np.random.default_rng(RANDOM_SEED)
        cost_mwh = pd.Series(rng.uniform(df["Costo_min"].values, df["Costo_max"].values), index=df.index)
    else:
        raise ValueError("COST_MODE debe ser 'mean' o 'uniform'.")

    df = df.copy()
    df["Costo_MWh"] = cost_mwh
    df["Costo_unit"] = cost_mwh * TO_MWH[ENERGY_UNIT.upper()]  # moneda por GWh (o por MWh)
    return df


# =============================================================================
# MODELO PYOMO
# =============================================================================
def build_model(df, residual_x):
    """Construye el modelo de despacho económico agregado diario."""
    techs = df["Tecnologia"].tolist()
    cost = dict(zip(df["Tecnologia"], df["Costo_unit"]))
    cap = dict(zip(df["Tecnologia"], df["Cap_max"]))
    emin = dict(zip(df["Tecnologia"], df["Min_tec"]))
    ens_penalty_unit = ENS_PENALTY * TO_MWH[ENERGY_UNIT.upper()]

    m = pyo.ConcreteModel(name="ED_residual_diario")

    # Conjuntos y parámetros
    m.T = pyo.Set(initialize=techs, ordered=True)
    m.cost = pyo.Param(m.T, initialize=cost)
    m.cap = pyo.Param(m.T, initialize=cap)
    m.emin = pyo.Param(m.T, initialize=emin)
    m.X = pyo.Param(initialize=residual_x)
    m.ens_pen = pyo.Param(initialize=ens_penalty_unit)

    # Variables
    m.e = pyo.Var(m.T, domain=pyo.NonNegativeReals)   # energía por tecnología
    m.ens = pyo.Var(domain=pyo.NonNegativeReals)      # energía no suministrada
    if not ALLOW_ENS:
        m.ens.fix(0.0)

    # Límites de energía (con o sin variable binaria de mínimo técnico)
    if USE_MIN_TECH and MIN_TECH_BINARY:
        m.u = pyo.Var(m.T, domain=pyo.Binary)
        m.lim_max = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] <= m.cap[t] * m.u[t])
        m.lim_min = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] >= m.emin[t] * m.u[t])
    else:
        m.lim_max = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] <= m.cap[t])
        if USE_MIN_TECH:
            m.lim_min = pyo.Constraint(m.T, rule=lambda m, t: m.e[t] >= m.emin[t])

    # Balance de energía del día
    m.balance = pyo.Constraint(expr=sum(m.e[t] for t in m.T) + m.ens == m.X)

    # Objetivo
    m.obj = pyo.Objective(
        expr=sum(m.cost[t] * m.e[t] for t in m.T) + m.ens_pen * m.ens,
        sense=pyo.minimize,
    )

    # Duales (solo disponibles en LP)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    return m


def get_solver():
    """Crea el solver y verifica que esté disponible."""
    kwargs = {}
    if SOLVER_EXECUTABLE:
        kwargs["executable"] = SOLVER_EXECUTABLE
    solver = pyo.SolverFactory(SOLVER_NAME, **kwargs)
    if not solver.available(exception_flag=False):
        raise RuntimeError(
            f"El solver '{SOLVER_NAME}' no está disponible. Instálelo (p. ej. CBC) y verifique el PATH, "
            f"o cambie SOLVER_NAME / SOLVER_EXECUTABLE al inicio del script."
        )
    return solver


# =============================================================================
# RESULTADOS
# =============================================================================
def report(m, df, results):
    """Imprime estado, costo, energía por tecnología, ENS, costo marginal y tabla resumen."""
    unit = ENERGY_UNIT
    term = results.solver.termination_condition
    status = results.solver.status

    print("\n" + "=" * 78)
    print(" RESULTADOS - DESPACHO ECONÓMICO DEL RESIDUAL DIARIO")
    print("=" * 78)
    print(f"Estado del solver        : {status}")
    print(f"Condición de terminación : {term}")

    if term not in (TerminationCondition.optimal, TerminationCondition.feasible) or status != SolverStatus.ok:
        print("\n[!] No se obtuvo una solución óptima.")
        if term in (TerminationCondition.infeasible, TerminationCondition.infeasibleOrUnbounded):
            print("    Posibles causas: capacidad total < residual con ALLOW_ENS=False, "
                  "o suma de mínimos técnicos > residual.")
        return

    total_cost = pyo.value(m.obj)
    ens = pyo.value(m.ens)
    energy = {t: pyo.value(m.e[t]) for t in m.T}

    # Costo marginal implícito: dual del balance (LP) o, si no hay duales (MILP),
    # costo de la tecnología marginal (la más cara con energía despachada y no en su tope).
    marginal_unit = None
    if not (USE_MIN_TECH and MIN_TECH_BINARY):
        try:
            marginal_unit = m.dual.get(m.balance, None)
        except Exception:
            marginal_unit = None
    marginal_tech = None
    tol = 1e-6
    candidates = df[[energy[t] > tol for t in df["Tecnologia"]]].copy() if len(df) else df
    if ens > tol:
        marginal_tech = "ENS (energía no suministrada)"
        approx_marg = ENS_PENALTY * TO_MWH[unit.upper()]
    elif not candidates.empty:
        idx = candidates["Costo_unit"].idxmax()
        marginal_tech = candidates.loc[idx, "Tecnologia"]
        approx_marg = candidates.loc[idx, "Costo_unit"]
    else:
        approx_marg = float("nan")
    if marginal_unit is None:
        marginal_unit = approx_marg

    marginal_mwh = marginal_unit / TO_MWH[unit.upper()]

    # Tabla resumen
    rows = []
    for _, r in df.iterrows():
        t = r["Tecnologia"]
        e = energy[t]
        rows.append({
            "Tecnologia": t,
            f"Costo ({CURRENCY}/MWh)": r["Costo_MWh"],
            f"Min ({unit})": r["Min_tec"],
            f"Cap_max ({unit})": r["Cap_max"],
            f"Energia ({unit})": e,
            "Uso (%)": 100.0 * e / r["Cap_max"] if r["Cap_max"] > 0 else 0.0,
            f"Costo total ({CURRENCY})": e * r["Costo_unit"],
        })
    if ALLOW_ENS:
        rows.append({
            "Tecnologia": "ENS",
            f"Costo ({CURRENCY}/MWh)": ENS_PENALTY,
            f"Min ({unit})": 0.0,
            f"Cap_max ({unit})": np.nan,
            f"Energia ({unit})": ens,
            "Uso (%)": np.nan,
            f"Costo total ({CURRENCY})": ens * pyo.value(m.ens_pen),
        })
    table = pd.DataFrame(rows)

    # Totales
    total_row = {c: "" for c in table.columns}
    total_row["Tecnologia"] = "TOTAL"
    total_row[f"Energia ({unit})"] = table[f"Energia ({unit})"].sum()
    total_row[f"Costo total ({CURRENCY})"] = table[f"Costo total ({CURRENCY})"].sum()
    table = pd.concat([table, pd.DataFrame([total_row])], ignore_index=True)

    print(f"\nResidual a cubrir (X)    : {pyo.value(m.X):,.3f} {unit}")
    print(f"Costo total óptimo       : {total_cost:,.2f} {CURRENCY}")
    print(f"Energía no suministrada  : {ens:,.3f} {unit}")
    print(f"Costo marginal implícito : {marginal_mwh:,.2f} {CURRENCY}/MWh "
          f"({marginal_unit:,.2f} {CURRENCY}/{unit})"
          + (f"  [tecnología marginal: {marginal_tech}]" if marginal_tech else ""))

    print("\nEnergía asignada por tecnología:")
    for t in m.T:
        print(f"  - {t:<25s}: {energy[t]:>12,.3f} {unit}")

    print("\nTabla resumen:")
    with pd.option_context("display.max_columns", None, "display.width", 200,
                           "display.float_format", lambda v: f"{v:,.2f}"):
        print(table.to_string(index=False))
    print("=" * 78)


# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        df = load_data(EXCEL_FILE)
        df = compute_costs(df)

        total_cap = df["Cap_max"].sum()
        print(f"Tecnologías leídas: {len(df)} | Capacidad total: {total_cap:,.2f} {ENERGY_UNIT} "
              f"| Residual X: {RESIDUAL_X:,.2f} {ENERGY_UNIT}")
        if RESIDUAL_X > total_cap and not ALLOW_ENS:
            print("[!] Advertencia: X supera la capacidad total y ALLOW_ENS=False -> modelo infactible.")

        model = build_model(df, RESIDUAL_X)
        solver = get_solver()
        results = solver.solve(model, tee=SOLVER_TEE)
        report(model, df, results)

    except (FileNotFoundError, ValueError, RuntimeError) as err:
        print(f"\n[ERROR] {err}", file=sys.stderr)
        sys.exit(1)
    except Exception as err:  # errores inesperados
        print(f"\n[ERROR inesperado] {type(err).__name__}: {err}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()


# =============================================================================
# FORMATO EXACTO DEL EXCEL DE EJEMPLO: costos_tecnologias.xlsx  (hoja 1)
# =============================================================================
# Fila 1 = encabezados (exactamente estos nombres). Costos en moneda/MWh.
# Capacidad_max y Minimo_tecnico deben terminar en _GWh o _MWh (el script
# convierte automáticamente a ENERGY_UNIT). Minimo_tecnico_* es opcional.
#
# | Tecnologia   | Costo_min | Costo_max | Capacidad_max_GWh | Minimo_tecnico_GWh |
# |--------------|-----------|-----------|-------------------|--------------------|
# | Solar        | 20000     | 60000     | 40                | 0                  |
# | Eolica       | 30000     | 80000     | 25                | 0                  |
# | Biomasa      | 150000    | 250000    | 20                | 0                  |
# | Carbon       | 200000    | 320000    | 120               | 30                 |
# | Gas          | 250000    | 400000    | 150               | 0                  |
# | Diesel       | 600000    | 900000    | 80                | 0                  |
#
# Notas:
#  - Si usa MWh, renombre las columnas a Capacidad_max_MWh y Minimo_tecnico_MWh.
#  - Si Costo en USD/MWh, cambie CURRENCY = "USD" al inicio del script.