from ..page import Page
from src.qt.mainwindow_ui import Ui_Nugget

from src.tweaks.tweak_loader import load_liquidglass
from src.tweaks.tweaks import TweakID

class LiquidGlassPage(Page):
    def __init__(self, ui: Ui_Nugget):
        super().__init__()
        self.ui = ui

    def load_page(self):
        # Create the radio buttons where needed
        self.createRadioBtns(key=TweakID.ForceSolariumFallback, container=self.ui.forceSolariumFallbackBtns)
        self.createRadioBtns(key=TweakID.DisableSolarium, container=self.ui.disableSolariumBtns)
        self.createRadioBtns(key=TweakID.IgnoreSolariumLinkedOnCheck, container=self.ui.ignoreSolariumAppBuildBtns)

        self.createRadioBtns(key=TweakID.NoLiquidClock, container=self.ui.noLiquidClockBtns)
        self.createRadioBtns(key=TweakID.NoLiquidDock, container=self.ui.noLiquidDockBtns)

        self.createRadioBtns(key=TweakID.DisableSpecularMotion, container=self.ui.disableSpecularBtns)
        self.createRadioBtns(key=TweakID.DisableOuterRefraction, container=self.ui.disableOuterRefractionBtns)
        self.createRadioBtns(key=TweakID.DisableSolariumHDR, container=self.ui.disableSolariumHDRBtns, invert_values=True)

        # iOS 27 additions
        self.createRadioBtns(key=TweakID.DisallowGlassButtons, container=self.ui.disallowGlassButtonsBtns)
        self.createRadioBtns(key=TweakID.DisallowGlassLockScreen, container=self.ui.disallowGlassLockScreenBtns)
        self.createRadioBtns(key=TweakID.ForceEnhancedSpeculars, container=self.ui.forceEnhancedSpecularsBtns)
        self.createRadioBtns(key=TweakID.ForceSolariumIntelligence, container=self.ui.forceSolariumIntelligenceBtns)
        self.createRadioBtns(key=TweakID.UISolariumFallback, container=self.ui.uiSolariumFallbackBtns)
        self.createRadioBtns(key=TweakID.IgnoreSolariumHardwareCheck, container=self.ui.ignoreSolariumHardwareCheckBtns)
        self.createRadioBtns(key=TweakID.IgnoreSolariumOptOut, container=self.ui.ignoreSolariumOptOutBtns)
        self.createRadioBtns(key=TweakID.DisableSpecularEverywhere, container=self.ui.disableSpecularEverywhereBtns)

        load_liquidglass()