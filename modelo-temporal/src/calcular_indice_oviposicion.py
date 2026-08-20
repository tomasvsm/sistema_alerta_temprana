"""
Calcula el indice de oviposicion estandarizado a partir del CSV crudo del
modelo (salida de run.py). Misma metodologia que calculo_indice_actividad.py
(tesis, espacializacion/): estandariza la columna "egg" contra su propia
ventana movil de 365 dias: (egg - min) / (max - min).

A diferencia del script original (que solo calculaba el indice para fechas
puntuales ya presentes como raster MCDA), esto lo calcula para TODAS las
fechas donde ya hay 365 dias de historia disponibles -- para que geoprocesos
despues busque el valor que necesite sin tener que volver a correr esto.
"""
import sys
import pandas as pd


def compute_series(df):
    """
    Parameters
    ----------
    df : pd.DataFrame
        Debe tener columnas 'date' (datetime) y 'egg' (float), ordenado por fecha.

    Returns
    -------
    pd.DataFrame
        Columnas: date, indice_oviposicion, std_semanal.
        Solo incluye fechas con >=365 dias de historia previa disponible.
    """
    df = df.sort_values('date').reset_index(drop=True)
    dates = df['date'].values
    egg = df['egg'].values

    out_dates, out_idx, out_std = [], [], []

    for i in range(len(df)):
        target_date = dates[i]
        window_start = target_date - pd.Timedelta(days=365)
        if window_start < dates[0]:
            continue  # todavia no hay un ano completo de historia

        window_mask = (dates >= window_start) & (dates <= target_date)
        window_egg = egg[window_mask]
        min_egg, max_egg = window_egg.min(), window_egg.max()

        if max_egg > min_egg:
            indice = (egg[i] - min_egg) / (max_egg - min_egg)
        else:
            indice = 0.0

        # std semanal: 7 indices diarios independientes, cada uno con su
        # propia ventana de 365 dias hacia atras
        week_start = target_date - pd.Timedelta(days=6)
        week_mask = (dates >= week_start) & (dates <= target_date)
        week_vals = []
        for d, e in zip(dates[week_mask], egg[week_mask]):
            d_window_mask = (dates >= d - pd.Timedelta(days=365)) & (dates <= d)
            d_window = egg[d_window_mask]
            d_min, d_max = d_window.min(), d_window.max()
            week_vals.append((e - d_min) / (d_max - d_min) if d_max > d_min else 0.0)
        std_semanal = pd.Series(week_vals).std() if len(week_vals) > 1 else 0.0

        out_dates.append(target_date)
        out_idx.append(round(indice, 6))
        out_std.append(round(float(std_semanal), 6) if pd.notna(std_semanal) else 0.0)

    return pd.DataFrame({
        'date': out_dates,
        'indice_oviposicion': out_idx,
        'std_semanal': out_std,
    })


def main(modelo_csv, output_csv):
    df = pd.read_csv(modelo_csv)
    df.columns = df.columns.str.strip()
    df['date'] = pd.to_datetime(df['date'])
    result = compute_series(df)
    result.to_csv(output_csv, index=False)
    print(f'{output_csv}: {len(result)} fechas con indice calculado '
          f'({result["date"].min().date()} -> {result["date"].max().date()})')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
