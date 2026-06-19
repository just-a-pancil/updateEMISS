import rpy2.robjects as ro
from rpy2.robjects.packages import importr

fedstat = importr("fedstatAPIr")

data = fedstat.fedstat_data_load_with_filters(
    indicator_id="31074",
    filters=ro.ListVector({"Год": "2023"})
)
