# -*- coding: utf-8 -*-
"""
Satellite Compare - QGIS plugin
Map (single date) or compare (two years) NDVI and Land Surface Temperature
(LST) from Landsat 8/9 Collection 2 Level-2 data.
Developed by Lena Alzahrani
"""
import os
import re
import tempfile
import traceback
from datetime import datetime

import numpy as np
from osgeo import gdal, ogr, osr

from qgis.PyQt.QtCore import Qt, QLocale
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import (QAction, QMessageBox, QApplication, QCheckBox, QLabel,
                                 QGroupBox, QRadioButton, QHBoxLayout, QButtonGroup,
                                 QDoubleSpinBox, QSpinBox, QBoxLayout, QWidget, QComboBox)
from qgis.core import (Qgis, QgsProject, QgsRasterLayer, QgsMapLayerProxyModel,
                       QgsColorRampShader, QgsRasterShader, QgsGeometry,
                       QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsSingleBandPseudoColorRenderer, QgsPalettedRasterRenderer)
from qgis.gui import QgsMapLayerComboBox

from .sat_compare_dialog import SatCompareDialog

# Landsat 8/9 Collection 2 Level-2 scale factors (USGS)
SR_SCALE, SR_OFFSET = 0.0000275, -0.2
ST_SCALE, ST_OFFSET = 0.00341802, 149.0
ST_QA_SCALE = 0.01          # ST_QA band = LST uncertainty, in kelvin x 100

# QA_PIXEL bits
QA_FILL_SNOW = (1 << 0) | (1 << 5)                      # removed as they are
QA_CLOUD = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)    # dilated cloud, cirrus, cloud, shadow (+ buffer)
QA_WATER = 1 << 7                                       # optional: dark basalt can be flagged as water

WARN_REMOVED_PCT = 50.0

# Landsat product id inside a file name, e.g. LC09_L2SP_169045_20250905_20250906_02_T1
LANDSAT_ID = re.compile(r'(L[COTEM]0\d)_(L[12][A-Z]{2})_(\d{3})(\d{3})_(\d{8})_\d{8}_\d{2}_(T1|T2|RT)')

AREA_WHOLE, AREA_VIEW, AREA_POLYGON = 0, 1, 2

# Single-date classes: (RGB colour, name)
SINGLE_CLASSES = {
    'NDVI': [((166, 97, 26), 'Bare / built-up / sand'),
             ((223, 194, 125), 'Sparse vegetation'),
             ((166, 217, 106), 'Moderate vegetation'),
             ((26, 150, 65), 'Dense vegetation')],
    'LST': [((44, 123, 182), 'Much cooler than average'),
            ((171, 217, 233), 'Cooler than average'),
            ((255, 255, 191), 'Near average'),
            ((253, 174, 97), 'Warmer than average'),
            ((215, 25, 28), 'Much warmer than average')],
}
LST_SIGMAS = (-1.5, -0.5, 0.5, 1.5)


# ---------------- raster helpers ----------------
def read_aligned(path, ref, src_nodata):
    """Read band 1 of `path` resampled onto the grid of `ref` (NaN = no data)."""
    gt = ref.GetGeoTransform()
    w, h = ref.RasterXSize, ref.RasterYSize
    bounds = (gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3])
    src = gdal.Open(path)
    if src_nodata is None:
        src_nodata = src.GetRasterBand(1).GetNoDataValue()
    opts = dict(format='MEM', outputBounds=bounds, width=w, height=h,
                dstSRS=ref.GetProjection(), resampleAlg='near',
                outputType=gdal.GDT_Float32, dstNodata=float('nan'))
    if src_nodata is not None:
        opts['srcNodata'] = src_nodata
    out = gdal.Warp('', src, **opts)
    return out.GetRasterBand(1).ReadAsArray().astype(np.float32)


def write_tif(path, arr, ref, dtype, nodata):
    ds = gdal.GetDriverByName('GTiff').Create(
        path, ref.RasterXSize, ref.RasterYSize, 1, dtype,
        options=['COMPRESS=DEFLATE', 'TILED=YES'])
    ds.SetGeoTransform(ref.GetGeoTransform())
    ds.SetProjection(ref.GetProjection())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(arr)
    band.FlushCache()
    ds = None


def pixel_area_km2(ds):
    srs = osr.SpatialReference(wkt=ds.GetProjection())
    if not srs.IsProjected() or abs(srs.GetLinearUnits() - 1.0) > 1e-9:
        return None
    gt = ds.GetGeoTransform()
    return abs(gt[1] * gt[5]) / 1e6


def crop_ref(ref, xmin, ymin, xmax, ymax):
    """In-memory grid: the part of `ref` (north-up) covering the given box, same pixels."""
    gt = ref.GetGeoTransform()
    c0 = max(0, int(np.floor((xmin - gt[0]) / gt[1])))
    c1 = min(ref.RasterXSize, int(np.ceil((xmax - gt[0]) / gt[1])))
    r0 = max(0, int(np.floor((ymax - gt[3]) / gt[5])))
    r1 = min(ref.RasterYSize, int(np.ceil((ymin - gt[3]) / gt[5])))
    if c1 <= c0 or r1 <= r0:
        raise ValueError('The analysis area does not overlap the image.')
    mem = gdal.GetDriverByName('MEM').Create('', c1 - c0, r1 - r0, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((gt[0] + c0 * gt[1], gt[1], 0.0, gt[3] + r0 * gt[5], 0.0, gt[5]))
    mem.SetProjection(ref.GetProjection())
    return mem


def rasterize_mask(ref, wkts):
    """Boolean array on the grid of `ref`: True inside the polygons (WKT, raster CRS)."""
    drv = ogr.GetDriverByName('Memory') or ogr.GetDriverByName('MEM')
    src = drv.CreateDataSource('aoi')
    lyr = src.CreateLayer('aoi', osr.SpatialReference(wkt=ref.GetProjection()), ogr.wkbUnknown)
    for wkt in wkts:
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        lyr.CreateFeature(feat)
        feat = None
    mem = gdal.GetDriverByName('MEM').Create('', ref.RasterXSize, ref.RasterYSize, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(ref.GetGeoTransform())
    mem.SetProjection(ref.GetProjection())
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
    return mem.GetRasterBand(1).ReadAsArray() == 1


def dilate(mask, radius):
    """Square binary dilation by `radius` pixels (pure numpy, no scipy needed)."""
    if radius <= 0 or not mask.any():
        return mask
    rows = mask.copy()
    for s in range(1, radius + 1):
        rows[s:, :] |= mask[:-s, :]
        rows[:-s, :] |= mask[s:, :]
    out = rows.copy()
    for s in range(1, radius + 1):
        out[:, s:] |= rows[:, :-s]
        out[:, :-s] |= rows[:, s:]
    return out


def landsat_info(path):
    """Scene details from a Landsat file name (+ acquisition time from the MTL file if present)."""
    m = LANDSAT_ID.search(os.path.basename(path))
    if not m:
        return None
    sensors = {'LC08': 'Landsat 8', 'LC09': 'Landsat 9'}
    info = {'id': m.group(0),
            'sensor': sensors.get(m.group(1), m.group(1)),
            'path_row': f'{m.group(3)}/{m.group(4)}',
            'date': datetime.strptime(m.group(5), '%Y%m%d').strftime('%Y-%m-%d'),
            'time': None}
    folder = os.path.dirname(path)
    try:
        for name in os.listdir(folder):
            if name.startswith(m.group(0)) and name.upper().endswith('_MTL.TXT'):
                with open(os.path.join(folder, name), encoding='utf-8', errors='ignore') as fh:
                    for line in fh:
                        if 'SCENE_CENTER_TIME' in line:
                            info['time'] = line.split('=', 1)[1].strip().strip('"')[:8] + ' UTC'
                            break
                break
    except OSError:
        pass
    return info


def wgs84_extent(ds, valid):
    """(lat_min, lat_max, lon_min, lon_max) of the valid pixels."""
    rows = np.where(valid.any(axis=1))[0]
    cols = np.where(valid.any(axis=0))[0]
    if rows.size == 0:
        return None
    gt = ds.GetGeoTransform()
    xs = [gt[0] + c * gt[1] for c in (cols[0], cols[-1] + 1)]
    ys = [gt[3] + r * gt[5] for r in (rows[0], rows[-1] + 1)]
    src = osr.SpatialReference(wkt=ds.GetProjection())
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    for srs in (src, dst):
        try:
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:
            pass
    ct = osr.CoordinateTransformation(src, dst)
    pts = [ct.TransformPoint(x, y)[:2] for x in xs for y in ys]
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lats), max(lats), min(lons), max(lons)


def style_ramp(layer, values_colors):
    """values_colors: list of (value, QColor, label), ascending."""
    lo, hi = values_colors[0][0], values_colors[-1][0]
    ramp = QgsColorRampShader(lo, hi)
    ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, c, t)
                               for v, c, t in values_colors])
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(lo)
    renderer.setClassificationMax(hi)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def style_diff(layer, index, span):
    if index == 'NDVI':
        low, high = QColor(215, 48, 39), QColor(26, 152, 80)
    else:
        low, high = QColor(69, 117, 180), QColor(215, 48, 39)
    style_ramp(layer, [(-span, low, f'{-span:.2f}'),
                       (0, QColor(255, 255, 191), '0'),
                       (span, high, f'+{span:.2f}')])


def style_value(layer, index, vals):
    lo, hi = (float(x) for x in np.percentile(vals, [2, 98]))
    if hi <= lo:
        hi = lo + 1e-6
    if index == 'NDVI':
        cols = [QColor(166, 97, 26), QColor(255, 255, 191), QColor(26, 150, 65)]
        fmt = '{:.2f}'
    else:
        cols = [QColor(44, 123, 182), QColor(255, 255, 191), QColor(215, 25, 28)]
        fmt = '{:.1f} °C'
    mid = (lo + hi) / 2
    style_ramp(layer, [(v, c, fmt.format(v)) for v, c in zip((lo, mid, hi), cols)])


def style_paletted(layer, classes):
    """classes: list of (pixel value, QColor, label)."""
    items = [QgsPalettedRasterRenderer.Class(v, c, t) for v, c, t in classes]
    layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, items))
    layer.triggerRepaint()


def style_class(layer, index):
    if index == 'NDVI':
        dec, inc = QColor(215, 48, 39), QColor(26, 152, 80)
        labels = ('Vegetation loss', 'No change', 'Vegetation gain')
    else:
        dec, inc = QColor(69, 117, 180), QColor(215, 48, 39)
        labels = ('Cooling', 'No change', 'Warming')
    style_paletted(layer, [(1, dec, labels[0]),
                           (2, QColor(220, 220, 220), labels[1]),
                           (3, inc, labels[2])])


def hex_of(rgb):
    return '#%02x%02x%02x' % rgb


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# ---------------- the plugin ----------------
class SatCompare:
    LAYER_NAMES = ('Ready', 'Red', 'Nir', 'Thermal', 'Qa', 'StQa')

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dlg = None
        self.mask_stats = {}

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, 'icon.png'))
        self.action = QAction(icon, 'Satellite Compare', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu('&Satellite Compare', self.action)

    def unload(self):
        self.iface.removePluginRasterMenu('&Satellite Compare', self.action)
        self.iface.removeToolBarIcon(self.action)

    # ----- dialog -----
    @classmethod
    def combos(cls, d):
        return [getattr(d, 'cmb' + n + y) for y in ('1', '2') for n in cls.LAYER_NAMES]

    @staticmethod
    def english_spin(spin):
        spin.setLocale(QLocale(QLocale.English, QLocale.UnitedStates))
        return spin

    def setup_dialog(self):
        d = SatCompareDialog()
        d.setLayoutDirection(Qt.LeftToRight)

        # header text from the .ui file
        for lbl in d.findChildren(QLabel):
            if 'between two years' in lbl.text():
                lbl.setText(lbl.text().replace('compare NDVI / LST between two years',
                                               'map or compare NDVI / LST'))

        # --- mode: single date or two-year comparison ---
        d.grpMode = QGroupBox()
        d.radioSingle = QRadioButton('Single date (map)')
        d.radioCompare = QRadioButton('Compare two years (change)')
        d.radioCompare.setChecked(True)
        d.modeGroup = QButtonGroup(d)
        mode_box = QHBoxLayout(d.grpMode)
        for r in (d.radioSingle, d.radioCompare):
            d.modeGroup.addButton(r)
            mode_box.addWidget(r)
        parent = d.grpInput.parentWidget()
        parent_layout = parent.layout() if parent is not None else None
        if isinstance(parent_layout, QBoxLayout):
            parent_layout.insertWidget(parent_layout.indexOf(d.grpInput), d.grpMode)
        else:
            d.formOptions.insertRow(0, d.grpMode)

        # --- QA_PIXEL and ST_QA pickers for each year ---
        for y in ('1', '2'):
            for name, text in (('Qa', 'QA (QA_PIXEL)'),
                               ('StQa', 'LST uncertainty (ST_QA, optional)')):
                lbl = QLabel(text)
                cmb = QgsMapLayerComboBox()
                setattr(d, 'lbl' + name + y, lbl)
                setattr(d, 'cmb' + name + y, cmb)
                getattr(d, 'formYear' + y).addRow(lbl, cmb)

        # --- analysis area ---
        d.lblArea = QLabel('Analysis area')
        d.cmbArea = QComboBox()
        d.cmbArea.addItems(['Whole image', 'Current map view', 'Polygon layer'])
        d.cmbArea.setToolTip('Polygon layer: uses the selected features if any are selected, '
                             'otherwise all features.')
        d.lblAreaLayer = QLabel('Area polygons')
        d.cmbAreaLayer = QgsMapLayerComboBox()
        try:
            d.cmbAreaLayer.setFilters(Qgis.LayerFilter.PolygonLayer)
        except AttributeError:
            d.cmbAreaLayer.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        d.formOptions.insertRow(0, d.lblArea, d.cmbArea)
        d.formOptions.insertRow(1, d.lblAreaLayer, d.cmbAreaLayer)

        # --- masking options ---
        d.chkMask = QCheckBox('Mask clouds, shadows and snow (uses QA_PIXEL)')
        d.chkMask.setChecked(True)
        d.formOptions.addRow(d.chkMask)

        d.chkWater = QCheckBox('Also mask water')
        d.chkWater.setChecked(True)
        d.chkWater.setToolTip('Turn off for inland areas without lakes or sea: very dark rock '
                              '(e.g. basalt lava fields) can be wrongly flagged as water.')
        d.formOptions.addRow(d.chkWater)

        d.lblBuffer = QLabel('Cloud buffer (pixels)')
        d.spinBuffer = self.english_spin(QSpinBox())
        d.spinBuffer.setRange(0, 30)
        d.spinBuffer.setValue(10)
        d.spinBuffer.setToolTip('Extra pixels removed around clouds and shadows (1 pixel = 30 m).')
        d.formOptions.addRow(d.lblBuffer, d.spinBuffer)

        d.lblStQaMax = QLabel('Max LST uncertainty (K)')
        d.spinStQaMax = self.english_spin(QDoubleSpinBox())
        d.spinStQaMax.setRange(0.5, 10.0)
        d.spinStQaMax.setSingleStep(0.5)
        d.spinStQaMax.setValue(2.0)
        d.spinStQaMax.setToolTip('Pixels whose ST_QA uncertainty is above this are removed.')
        d.formOptions.addRow(d.lblStQaMax, d.spinStQaMax)

        d.lblNdviLimits = QLabel('NDVI class limits')
        d.ndviLimits = QWidget()
        limits_box = QHBoxLayout(d.ndviLimits)
        limits_box.setContentsMargins(0, 0, 0, 0)
        d.spinT1, d.spinT2, d.spinT3 = (self.english_spin(QDoubleSpinBox()) for _ in range(3))
        for spin, value in ((d.spinT1, 0.2), (d.spinT2, 0.4), (d.spinT3, 0.6)):
            spin.setRange(-1.0, 1.0)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
            spin.setValue(value)
            limits_box.addWidget(spin)
        d.ndviLimits.setToolTip('Bare < limit 1 <= sparse < limit 2 <= moderate < limit 3 <= dense.\n'
                                'In arid areas natural vegetation can be lower than 0.2.')
        d.formOptions.addRow(d.lblNdviLimits, d.ndviLimits)

        d.lblLstNote = QLabel('LST classes: mean ± 0.5σ and ± 1.5σ of the analysis area.')
        d.lblLstNote.setWordWrap(True)
        d.formOptions.addRow(d.lblLstNote)

        for w in (d.spinYear1, d.spinYear2, d.spinThreshold):
            self.english_spin(w)
        try:
            raster_filter = Qgis.LayerFilter.RasterLayer
        except AttributeError:
            raster_filter = QgsMapLayerProxyModel.RasterLayer
        for cmb in self.combos(d):
            cmb.setFilters(raster_filter)
            cmb.setAllowEmptyLayer(True)
            cmb.setLayer(None)

        for widget in (d.radioReady, d.radioNdvi, d.chkMask, d.radioSingle):
            widget.toggled.connect(lambda _: self.update_visibility())
        d.cmbArea.currentIndexChanged.connect(lambda _: self.update_visibility())
        d.radioNdvi.toggled.connect(lambda _: self.update_threshold())
        self.dlg = d
        self.update_visibility()

    def single(self):
        return self.dlg.radioSingle.isChecked()

    def needed(self):
        """Required layer pickers for the current choices (ST_QA is optional and handled apart)."""
        d = self.dlg
        if d.radioReady.isChecked():
            return ['Ready']
        names = ['Red', 'Nir'] if d.radioNdvi.isChecked() else ['Thermal']
        if d.chkMask.isChecked() or d.chkWater.isChecked():
            names.append('Qa')
        return names

    def update_visibility(self):
        d = self.dlg
        single = self.single()
        raw = d.radioRaw.isChecked()
        ndvi = d.radioNdvi.isChecked()
        needed = self.needed()
        for y in ('1', '2'):
            for name in self.LAYER_NAMES:
                show = (raw and not ndvi) if name == 'StQa' else name in needed
                getattr(d, 'lbl' + name + y).setVisible(show)
                getattr(d, 'cmb' + name + y).setVisible(show)
        d.grpYear2.setVisible(not single)

        polygon = d.cmbArea.currentIndex() == AREA_POLYGON
        d.lblAreaLayer.setVisible(polygon)
        d.cmbAreaLayer.setVisible(polygon)
        d.chkMask.setVisible(raw)
        d.chkWater.setVisible(raw)
        for w in (d.lblBuffer, d.spinBuffer):
            w.setVisible(raw and d.chkMask.isChecked())
        for w in (d.lblStQaMax, d.spinStQaMax):
            w.setVisible(raw and not ndvi)
        thr_label = d.formOptions.labelForField(d.spinThreshold)
        for w in (thr_label, d.spinThreshold):
            if w is not None:
                w.setVisible(not single)
        for w in (d.lblNdviLimits, d.ndviLimits):
            w.setVisible(single and ndvi)
        d.lblLstNote.setVisible(single and not ndvi)

        d.grpMode.setTitle('Step 1: Mode')
        d.grpInput.setTitle('Step 2: Input type')
        d.grpIndex.setTitle('Step 3: Index')
        d.grpYear1.setTitle('Step 4: Image' if single else 'Step 4: First year')
        d.grpYear2.setTitle('Step 5: Second year')
        d.grpOptions.setTitle('Step 5: Options' if single else 'Step 6: Options')
        d.adjustSize()

    def update_threshold(self):
        # sensible defaults: 0.10 NDVI units, 2 degrees C for LST
        self.dlg.spinThreshold.setValue(0.10 if self.dlg.radioNdvi.isChecked() else 2.0)

    def validate(self):
        d = self.dlg
        years = ('1',) if self.single() else ('1', '2')
        for y in years:
            for n in self.needed():
                if getattr(d, 'cmb' + n + y).currentLayer() is None:
                    what = 'the image' if self.single() else f'year {y}'
                    return f'Please choose all the required layers for {what}.'
        if not self.single() and d.spinYear1.value() == d.spinYear2.value():
            return 'The two years must be different.'
        if self.single() and d.radioNdvi.isChecked():
            if not d.spinT1.value() < d.spinT2.value() < d.spinT3.value():
                return 'NDVI class limits must increase (limit 1 < limit 2 < limit 3).'
        if d.cmbArea.currentIndex() == AREA_POLYGON and d.cmbAreaLayer.currentLayer() is None:
            return 'Please choose a polygon layer for the analysis area.'
        return None

    # ----- run -----
    def run(self):
        if self.dlg is None:
            self.setup_dialog()
        while True:
            if not self.dlg.exec_():
                return
            problem = self.validate()
            if not problem:
                break
            QMessageBox.warning(self.iface.mainWindow(), 'Satellite Compare', problem)

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            summary = self.process_single() if self.single() else self.process_compare()
        except Exception:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self.iface.mainWindow(), 'Satellite Compare',
                                 'Something went wrong:\n\n' + traceback.format_exc())
            return
        QApplication.restoreOverrideCursor()
        self.show_result(summary)

    def show_result(self, text):
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle('Satellite Compare - results')
        box.setIcon(QMessageBox.Information)
        box.setTextFormat(Qt.RichText if text.lstrip().startswith('<') else Qt.PlainText)
        box.setText(text)
        box.exec_()

    # ----- analysis area -----
    def analysis_area(self, ref):
        """Return (grid to work on, polygon mask or None, description)."""
        d = self.dlg
        mode = d.cmbArea.currentIndex()
        if mode == AREA_WHOLE:
            return ref, None, 'Whole image'
        raster_crs = QgsCoordinateReferenceSystem.fromWkt(ref.GetProjection())
        project = QgsProject.instance()
        if mode == AREA_VIEW:
            canvas = self.iface.mapCanvas()
            xform = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), raster_crs, project)
            r = xform.transformBoundingBox(canvas.extent())
            grid = crop_ref(ref, r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
            return grid, None, 'Current map view'

        layer = d.cmbAreaLayer.currentLayer()
        selected = layer.selectedFeatureCount() > 0
        feats = layer.getSelectedFeatures() if selected else layer.getFeatures()
        xform = QgsCoordinateTransform(layer.crs(), raster_crs, project)
        geoms = []
        for f in feats:
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            g.transform(xform)
            geoms.append(g)
        if not geoms:
            raise ValueError('The polygon layer has no geometries.')
        box = geoms[0].boundingBox()
        for g in geoms[1:]:
            box.combineExtentWith(g.boundingBox())
        grid = crop_ref(ref, box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
        mask = rasterize_mask(grid, [g.asWkt() for g in geoms])
        if not mask.any():
            raise ValueError('The polygons do not cover any pixel of the image.')
        which = 'selected' if selected else 'all'
        return grid, mask, f"Polygon layer '{layer.name()}' ({len(geoms)} {which} feature(s))"

    # ----- processing -----
    @staticmethod
    def path_of(cmb):
        return cmb.currentLayer().source().split('|')[0]

    def use_qa(self):
        d = self.dlg
        return d.radioRaw.isChecked() and (d.chkMask.isChecked() or d.chkWater.isChecked())

    def use_stqa(self, y):
        d = self.dlg
        return (d.radioRaw.isChecked() and not d.radioNdvi.isChecked()
                and getattr(d, 'cmbStQa' + y).currentLayer() is not None)

    def load_index(self, y, ref, aoi):
        d = self.dlg
        get = lambda n: self.path_of(getattr(d, 'cmb' + n + y))
        if d.radioReady.isChecked():
            value = read_aligned(get('Ready'), ref, None)
        elif d.radioNdvi.isChecked():
            red = read_aligned(get('Red'), ref, 0) * SR_SCALE + SR_OFFSET
            nir = read_aligned(get('Nir'), ref, 0) * SR_SCALE + SR_OFFSET
            with np.errstate(divide='ignore', invalid='ignore'):
                value = (nir - red) / (nir + red)
            value[~np.isfinite(value)] = np.nan
            value = np.clip(value, -1, 1)
        else:
            kelvin = read_aligned(get('Thermal'), ref, 0) * ST_SCALE + ST_OFFSET
            value = kelvin - 273.15

        if aoi is not None:
            value[~aoi] = np.nan
        stats = {'data': int(np.isfinite(value).sum()), 'water': 0, 'cloud': 0, 'stqa': 0}
        if stats['data'] == 0:
            raise ValueError('The analysis area has no image data'
                             + ('' if self.single() else f' for year {y}') + '.')

        def remove(key, bad):
            hit = bad & np.isfinite(value)
            stats[key] += int(hit.sum())
            value[hit] = np.nan

        if self.use_qa():
            qa = read_aligned(get('Qa'), ref, None)
            qa_int = np.nan_to_num(qa, nan=1).astype(np.uint16)
            if d.chkWater.isChecked():
                remove('water', (qa_int & QA_WATER) != 0)
            if d.chkMask.isChecked():
                cloud = dilate((qa_int & QA_CLOUD) != 0, d.spinBuffer.value())
                remove('cloud', cloud | ((qa_int & QA_FILL_SNOW) != 0))
        if self.use_stqa(y):
            uncertainty = read_aligned(get('StQa'), ref, -9999) * ST_QA_SCALE
            with np.errstate(invalid='ignore'):
                remove('stqa', uncertainty > d.spinStQaMax.value())
        stats['kept'] = int(np.isfinite(value).sum())
        self.mask_stats[y] = stats
        return value

    def removal(self, y):
        """(text line, removed percent) for one image."""
        s = self.mask_stats.get(y)
        if not s or not s['data']:
            return None, 0.0
        pct = 100.0 * (s['data'] - s['kept']) / s['data']
        parts = [f'{label} {100.0 * s[key] / s["data"]:.1f}%'
                 for key, label in (('water', 'water'), ('cloud', 'clouds/shadows/snow'),
                                    ('stqa', 'high LST uncertainty')) if s[key]]
        line = f'Removed by masking: {pct:.1f}% of the area'
        if parts:
            line += ' (' + ', '.join(parts) + ')'
        return line, pct

    @staticmethod
    def warning_text(pct, what='the image'):
        return (f'WARNING: {pct:.0f}% of {what} was removed by masking. The result may not '
                f'represent the whole area; try a clearer image or another date.')

    def output_dir(self):
        out_dir = self.dlg.fileOutput.filePath() or tempfile.mkdtemp(prefix='sat_compare_')
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def masking_note(self, y_list):
        d = self.dlg
        notes = []
        if d.radioRaw.isChecked():
            if d.chkMask.isChecked():
                notes.append(f'Clouds, shadows and snow were masked (QA_PIXEL), '
                             f'with a {d.spinBuffer.value()}-pixel buffer around clouds.')
            notes.append('Water was masked (QA_PIXEL).' if d.chkWater.isChecked()
                         else 'Water was not masked.')
        if any(self.use_stqa(y) for y in y_list):
            notes.append(f'LST pixels with uncertainty above {d.spinStQaMax.value():g} K '
                         f'were removed (ST_QA).')
        return notes

    def add_group(self, title):
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, title)

        def add(path, name, visible=True):
            lyr = QgsRasterLayer(path, name)
            project.addMapLayer(lyr, False)
            group.addLayer(lyr).setItemVisibilityChecked(visible)
            return lyr
        return add

    # ----- single date -----
    def process_single(self):
        gdal.UseExceptions()
        ogr.UseExceptions()
        d = self.dlg
        self.mask_stats = {}
        index = 'NDVI' if d.radioNdvi.isChecked() else 'LST'
        year = d.spinYear1.value()
        out_dir = self.output_dir()

        first_path = self.path_of(getattr(d, 'cmb' + self.needed()[0] + '1'))
        ref, aoi, area_desc = self.analysis_area(gdal.Open(first_path))
        v = self.load_index('1', ref, aoi)
        valid = np.isfinite(v)
        if not valid.any():
            raise ValueError('No valid pixels left after masking.')
        vals = v[valid].astype(np.float64)
        mean, sd = float(vals.mean()), float(vals.std())

        if index == 'NDVI':
            bounds = [d.spinT1.value(), d.spinT2.value(), d.spinT3.value()]
            method = 'Fixed NDVI limits (adjustable in Options)'
            unit, fmt = '', '{:.2f}'
        else:
            bounds = [mean + k * sd for k in LST_SIGMAS]
            method = f'Mean ± 0.5σ and ± 1.5σ of the analysis area (σ = {sd:.2f} °C)'
            unit, fmt = ' °C', '{:.1f}'

        cls = np.zeros(v.shape, dtype=np.uint8)
        cls[valid] = (np.digitize(v[valid], bounds) + 1).astype(np.uint8)

        n = int(valid.sum())
        px_km2 = pixel_area_km2(ref)
        edges = [None] + bounds + [None]
        rows = []
        for k, (rgb, name) in enumerate(SINGLE_CLASSES[index], start=1):
            lo, hi = edges[k - 1], edges[k]
            if lo is None:
                rng = f'< {fmt.format(hi)}{unit}'
            elif hi is None:
                rng = f'≥ {fmt.format(lo)}{unit}'
            else:
                rng = f'{fmt.format(lo)} – {fmt.format(hi)}{unit}'
            count = int(np.count_nonzero(cls == k))
            rows.append({'value': k, 'rgb': rgb, 'name': name, 'range': rng,
                         'pct': 100.0 * count / n,
                         'km2': count * px_km2 if px_km2 else None})

        # --- write and add layers ---
        tag = f'{index}_{year}_single_{datetime.now():%H%M%S}'
        value_path = os.path.join(out_dir, f'{tag}_value.tif')
        cls_path = os.path.join(out_dir, f'{tag}_class.tif')
        write_tif(value_path, np.where(valid, v, -9999), ref, gdal.GDT_Float32, -9999)
        write_tif(cls_path, cls, ref, gdal.GDT_Byte, 0)

        add = self.add_group(f'Satellite Compare - {index} {year}')
        cls_lyr = add(cls_path, f'{index} {year} classes')
        val_lyr = add(value_path, f'{index} {year} values', False)
        style_paletted(cls_lyr, [(r['value'], QColor(*r['rgb']), f"{r['name']} ({r['range']})")
                                 for r in rows])
        style_value(val_lyr, index, vals)
        self.iface.setActiveLayer(cls_lyr)
        self.iface.zoomToActiveLayer()

        # --- scene details ---
        info = landsat_info(first_path)
        extent = wgs84_extent(ref, valid)
        title = 'NDVI (vegetation)' if index == 'NDVI' else 'Land Surface Temperature (LST)'
        area_lines = []
        if info:
            area_lines += [f"Scene: {info['id']}",
                           f"Satellite: {info['sensor']}   Path/Row: {info['path_row']}",
                           f"Date: {info['date']}" + (f"   Time: {info['time']}" if info['time'] else '')]
        else:
            area_lines.append(f'Input: {os.path.basename(first_path)}')
        area_lines.append(f'Analysis area: {area_desc}')
        if px_km2:
            area_lines.append(f'Area analysed: {n * px_km2:,.0f} km2 (after masking)')
        if extent:
            area_lines.append('Extent: {:.3f}°–{:.3f}° N, {:.3f}°–{:.3f}° E'.format(*extent))
        removed_line, removed_pct = self.removal('1')
        if removed_line:
            area_lines.append(removed_line)
        warning = self.warning_text(removed_pct) if removed_pct > WARN_REMOVED_PCT else None

        stat_lines = [f'Mean: {fmt.format(mean)}{unit}',
                      f'Min: {fmt.format(vals.min())}{unit}   Max: {fmt.format(vals.max())}{unit}',
                      f'Standard deviation: {fmt.format(sd)}{unit}',
                      f'Classes: {method}']
        notes = self.masking_note(['1'])
        if index == 'LST':
            notes.append('LST is the temperature of the ground surface at the satellite '
                         'overpass, not air temperature.')

        # --- HTML for the dialog ---
        html = [f'<h3>{title} – single date</h3>']
        if warning:
            html.append(f"<p><b><font color='#c0392b'>{esc(warning)}</font></b></p>")
        html += ['<b>Area</b><br>' + '<br>'.join(esc(s) for s in area_lines),
                 '<br><br><b>Statistics</b><br>' + '<br>'.join(esc(s) for s in stat_lines),
                 '<br><br><table border="1" cellspacing="0" cellpadding="4">',
                 '<tr><th></th><th>Class</th><th>Range</th><th>Share</th><th>Area</th></tr>']
        for r in rows:
            km2 = f"{r['km2']:,.1f} km2" if r['km2'] is not None else '-'
            html.append(f"<tr><td bgcolor='{hex_of(r['rgb'])}' width='22'>&nbsp;</td>"
                        f"<td>{esc(r['name'])}</td><td>{esc(r['range'])}</td>"
                        f"<td align='right'>{r['pct']:.1f}%</td><td align='right'>{km2}</td></tr>")
        html.append('</table>')
        if notes:
            html.append('<br>' + '<br>'.join(esc(s) for s in notes))
        html.append(f'<br><br>Files saved in:<br>{esc(out_dir)}')

        # --- text + CSV copies next to the outputs ---
        text = [f'{title} - single date', '']
        if warning:
            text += [warning, '']
        text += area_lines + [''] + stat_lines + ['']
        text += [f"{r['name']}: {r['range']}  {r['pct']:.1f}%"
                 + (f"  ({r['km2']:,.1f} km2)" if r['km2'] is not None else '') for r in rows]
        text += [''] + notes
        with open(os.path.join(out_dir, f'{tag}_summary.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(text))
        with open(os.path.join(out_dir, f'{tag}_classes.csv'), 'w', encoding='utf-8-sig') as f:
            f.write('class_value,class,range,share_percent,area_km2,colour\n')
            for r in rows:
                km2 = f"{r['km2']:.3f}" if r['km2'] is not None else ''
                f.write(f"{r['value']},{r['name']},{r['range']},{r['pct']:.2f},{km2},{hex_of(r['rgb'])}\n")
        return ''.join(html)

    # ----- two-year comparison -----
    def process_compare(self):
        gdal.UseExceptions()
        ogr.UseExceptions()
        d = self.dlg
        self.mask_stats = {}
        y1, y2 = d.spinYear1.value(), d.spinYear2.value()
        index = 'NDVI' if d.radioNdvi.isChecked() else 'LST'
        thr = d.spinThreshold.value()
        out_dir = self.output_dir()

        first = gdal.Open(self.path_of(getattr(d, 'cmb' + self.needed()[0] + '1')))
        ref, aoi, area_desc = self.analysis_area(first)
        v1 = self.load_index('1', ref, aoi)
        v2 = self.load_index('2', ref, aoi)
        diff = v2 - v1
        valid = np.isfinite(diff)
        if not valid.any():
            raise ValueError('The two images do not overlap (no common valid pixels).')

        cls = np.zeros(diff.shape, dtype=np.uint8)
        with np.errstate(invalid='ignore'):
            cls[valid & (diff < -thr)] = 1
            cls[valid & (np.abs(diff) <= thr)] = 2
            cls[valid & (diff > thr)] = 3

        tag = f'{index}_{y1}_{y2}_{datetime.now():%H%M%S}'
        diff_path = os.path.join(out_dir, f'{tag}_difference.tif')
        cls_path = os.path.join(out_dir, f'{tag}_change_class.tif')
        write_tif(diff_path, np.where(valid, diff, -9999), ref, gdal.GDT_Float32, -9999)
        write_tif(cls_path, cls, ref, gdal.GDT_Byte, 0)
        extra = []
        if d.radioRaw.isChecked():
            for y, v in ((y1, v1), (y2, v2)):
                p = os.path.join(out_dir, f'{index}_{y}_{datetime.now():%H%M%S}.tif')
                write_tif(p, np.where(np.isfinite(v), v, -9999), ref, gdal.GDT_Float32, -9999)
                extra.append(p)

        add = self.add_group(f'Satellite Compare - {index} {y1}-{y2}')
        cls_lyr = add(cls_path, f'{index} change class')
        diff_lyr = add(diff_path, f'{index} difference', False)
        for p in extra:
            add(p, os.path.splitext(os.path.basename(p))[0], False)
        style_class(cls_lyr, index)
        span = float(np.percentile(np.abs(diff[valid]), 98)) or thr
        style_diff(diff_lyr, index, span)
        self.iface.setActiveLayer(cls_lyr)
        self.iface.zoomToActiveLayer()

        unit = '' if index == 'NDVI' else ' C'
        n = int(valid.sum())
        px_km2 = pixel_area_km2(ref)

        def share(k, label):
            count = int(np.count_nonzero(cls == k))
            text = f'{label}: {100.0 * count / n:.1f}%'
            if px_km2:
                text += f'  ({count * px_km2:,.2f} km2)'
            return text

        warnings = []
        removal_lines = []
        for key, year in (('1', y1), ('2', y2)):
            line, pct = self.removal(key)
            if line:
                removal_lines.append(f'{year} - {line}')
            if pct > WARN_REMOVED_PCT:
                warnings.append(self.warning_text(pct, f'the {year} image'))

        lines = [f'{index} change {y1} -> {y2}', '']
        if warnings:
            lines += warnings + ['']
        lines += [f'Analysis area: {area_desc}']
        if px_km2:
            lines.append(f'Area compared: {n * px_km2:,.0f} km2 (valid in both years)')
        lines += removal_lines + ['',
                                  f'Mean {y1}: {np.mean(v1[valid]):.3f}{unit}',
                                  f'Mean {y2}: {np.mean(v2[valid]):.3f}{unit}',
                                  f'Mean change: {np.mean(diff[valid]):+.3f}{unit}', '',
                                  share(1, f'Decrease (< -{thr:g})'),
                                  share(2, 'No change'),
                                  share(3, f'Increase (> +{thr:g})')]
        notes = self.masking_note(['1', '2'])
        if index == 'LST':
            notes.append('LST differences between two single days include weather effects; '
                         'compare images taken close in day of year and in clear, calm conditions.')
        if notes:
            lines += [''] + notes
        lines += ['', f'Files saved in:\n{out_dir}']
        summary = '\n'.join(lines)
        with open(os.path.join(out_dir, f'{tag}_summary.txt'), 'w', encoding='utf-8') as f:
            f.write(summary)
        return summary