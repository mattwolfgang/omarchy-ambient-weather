import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.mattwolfgang.ambient-weather"
  ipcTarget: "io.github.mattwolfgang.ambient-weather"
  manageIpc: false

  property var anchorItem: null
  // The bar identifies panels by the widget mounted in its slot
  // (BarWidget.qml), so hand that to KeyboardPanel as the owner.
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  // Keys, station and location live in config.json (see fetch.py); only
  // the refresh interval is a shell.json setting.
  readonly property int refreshMinutes: Math.max(2, parseInt(setting("refreshMinutes", 5), 10) || 5)
  readonly property string fetchScript: String(Qt.resolvedUrl("fetch.py")).replace(/^file:\/\//, "")

  // ---- Data. Last good values are kept when a fetch fails.
  property var station: null
  property string stationError: ""
  property bool needsSetup: false
  property var forecast: null
  property string forecastError: ""
  property bool loading: false

  readonly property var periods: forecast && forecast.periods ? forecast.periods : []
  readonly property string forecastPlace: forecast && forecast.place ? forecast.place : ""
  readonly property string currentIcon: periods.length > 0 ? periods[0].icon : ""

  // Bar label: forecast condition glyph plus the station's outdoor temp
  // (falling back to the forecast temp when the station is unavailable).
  readonly property string label: {
    var t = station && station.tempF !== null ? station.tempF : (periods.length > 0 ? periods[0].temp : null)
    if (t === null || t === undefined) return currentIcon !== "" ? currentIcon : (needsSetup ? "\uf013" : "")
    return currentIcon + "  " + Math.round(t) + "°"
  }

  readonly property color dim: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.5)
  readonly property color fg: root.bar ? root.bar.foreground : Color.foreground
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family

  // ---- Settings view state.
  property bool showSettings: false
  property var savedConfig: null
  property string settingsMessage: ""
  property bool settingsError: false
  property var locationResults: []
  property bool saving: saveProc.running

  function open() {
    root.controller.show()
    refresh()
  }

  function close() {
    root.controller.hide()
    closeSettings()
  }
  function toggle() { root.opened ? root.close() : root.open() }

  function refresh() {
    if (fetchProc.running) return
    loading = true
    fetchProc.command = ["python3", fetchScript]
    fetchProc.running = true
  }

  function openSettings() {
    if (!root.opened) root.controller.show()
    settingsMessage = ""
    settingsError = false
    locationResults = []
    apiKeyField.text = ""
    appKeyField.text = ""
    showSettings = true
    configProc.running = true
    Qt.callLater(function() { appKeyField.field.forceActiveFocus() })
  }

  function closeSettings() {
    showSettings = false
    apiKeyField.text = ""
    appKeyField.text = ""
    keyCatcher.forceActiveFocus()
  }

  function fillSettings(config) {
    savedConfig = config
    macField.text = config.deviceMac || ""
    ipField.text = config.stationIp || ""
    locationField.text = config.locationName || ""
    latField.text = config.latitude || ""
    lonField.text = config.longitude || ""
  }

  function searchLocation() {
    var q = locationField.text.trim()
    if (q.length < 2 || geocodeProc.running) return
    locationResults = []
    settingsMessage = "Searching…"
    settingsError = false
    geocodeProc.command = ["python3", fetchScript, "--geocode", q]
    geocodeProc.running = true
  }

  function pickLocation(place) {
    locationField.text = place.name
    latField.text = String(place.latitude)
    lonField.text = String(place.longitude)
    locationResults = []
    settingsMessage = ""
  }

  function saveSettings() {
    if (saving) return
    settingsMessage = "Saving…"
    settingsError = false
    // Keys travel over stdin, never argv. Blank key fields keep the saved keys.
    saveProc.payload = JSON.stringify({
      apiKey: apiKeyField.text.trim(),
      applicationKey: appKeyField.text.trim(),
      deviceMac: macField.text.trim(),
      stationIp: ipField.text.trim(),
      locationName: locationField.text.trim(),
      latitude: latField.text.trim(),
      longitude: lonField.text.trim()
    })
    saveProc.command = ["python3", fetchScript, "--save-config"]
    saveProc.running = true
  }

  function fmt(value, digits, suffix) {
    if (value === null || value === undefined) return "—"
    return Number(value).toFixed(digits) + (suffix || "")
  }

  function compass(deg) {
    if (deg === null || deg === undefined) return ""
    var dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16]
  }

  function observedText() {
    if (!station || !station.observedAt) return ""
    var d = new Date(station.observedAt)
    var mins = Math.round((Date.now() - d.getTime()) / 60000)
    return "Updated " + Qt.formatTime(d, "h:mm AP") + (mins >= 2 ? " (" + mins + " min ago)" : "")
  }

  Process {
    id: fetchProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.loading = false
        try {
          var data = JSON.parse(String(text || ""))
          if (data.station && !data.station.error) {
            root.station = data.station
            root.stationError = ""
            root.needsSetup = false
          } else {
            root.stationError = data.station ? data.station.error : "No station data"
            root.needsSetup = !!(data.station && data.station.needsSetup)
            if (root.needsSetup) root.station = null
          }
          if (data.forecast && !data.forecast.error) {
            root.forecast = data.forecast
            root.forecastError = ""
          } else {
            root.forecastError = data.forecast ? data.forecast.error : "No forecast data"
          }
        } catch (e) {
          root.stationError = root.forecastError = "Fetch failed"
        }
      }
    }
    onExited: function(code) { if (code !== 0) root.loading = false }
  }

  Process {
    id: configProc
    command: ["python3", root.fetchScript, "--show-config"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.fillSettings(JSON.parse(String(text || "")).config || {}) } catch (e) {}
      }
    }
  }

  Process {
    id: saveProc
    property string payload: ""
    stdinEnabled: true
    onStarted: {
      write(payload + "\n")
      payload = ""
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var result = null
        try { result = JSON.parse(String(text || "")) } catch (e) {}
        if (result && result.ok) {
          root.fillSettings(result.config || {})
          root.settingsMessage = result.message || "Saved"
          root.settingsError = false
          apiKeyField.text = ""
          appKeyField.text = ""
          root.station = null
          root.forecast = null
          root.refresh()
          if (!result.message || result.message === "Saved") root.closeSettings()
        } else {
          root.settingsMessage = result && result.message ? result.message : "Couldn't save settings"
          root.settingsError = true
        }
      }
    }
  }

  Process {
    id: geocodeProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var result = null
        try { result = JSON.parse(String(text || "")) } catch (e) {}
        root.locationResults = result && result.results ? result.results : []
        root.settingsError = !!(result && result.error) || root.locationResults.length === 0
        root.settingsMessage = result && result.error ? result.error
          : (root.locationResults.length === 0 ? "No places found" : "")
      }
    }
  }

  Timer {
    interval: root.refreshMinutes * 60 * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
    function settings(): void { root.openSettings() }
  }

  // ---- Small reusable pieces.
  component Caption: Text {
    textFormat: Text.PlainText
    color: root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    font.letterSpacing: 1
  }

  component Stat: Column {
    property string title: ""
    property string value: ""
    spacing: Style.space(3)
    Caption { text: parent.title }
    Text {
      textFormat: Text.PlainText
      text: parent.value
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.title
    }
  }

  // A labelled text input for the settings form.
  component Field: Column {
    property alias label: fieldLabel.text
    property alias hint: fieldHint.text
    property alias field: input
    property alias text: input.text
    property alias placeholderText: input.placeholderText
    property alias password: input.password
    signal accepted()
    spacing: Style.space(4)
    Caption { id: fieldLabel }
    TextField {
      id: input
      width: parent.width
      foreground: root.fg
      font.family: root.fontFamily
      onAccepted: parent.accepted()
    }
    Caption {
      id: fieldHint
      visible: text !== ""
      width: parent.width
      wrapMode: Text.WordWrap
      font.letterSpacing: 0
      font.italic: true
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(760))
    contentHeight: panel.fittedContentHeight(scroll.contentHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // The settings form needs every key for typing and Tab between fields.
      blocked: root.showSettings
      onReturnRequested: root.refresh()
      onCloseRequested: root.close()
      onTabRequested: function(direction) {
        if (root.bar && typeof root.bar.switchPanelFrom === "function") root.bar.switchPanelFrom(root.barIdentity, direction)
      }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: root.showSettings ? settingsForm.implicitHeight
          : Math.max(stationColumn.implicitHeight, forecastColumn.implicitHeight)
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        // Settings toggle, top-right corner of the panel.
        PanelActionButton {
          z: 2
          x: scroll.width - width - Style.space(16)
          iconText: root.showSettings ? "\uf00d" : "\uf013" // nf-fa-close / nf-fa-cog
          tooltipText: root.showSettings ? "Close settings" : "Settings"
          foreground: root.dim
          hoverColor: root.fg
          fontFamily: root.fontFamily
          onClicked: root.showSettings ? root.closeSettings() : root.openSettings()
        }

        Item {
          visible: !root.showSettings
          width: scroll.width
          height: scroll.contentHeight

          // ================= Left: weather station =================
          Column {
            id: stationColumn
            x: Style.space(16)
            width: scroll.width * 0.46 - Style.space(16)
            spacing: Style.space(14)

            Row {
              spacing: Style.space(6)
              Caption { text: "\uf015"; font.pixelSize: Style.font.body } // nf-fa-home
              Caption { text: (root.station ? root.station.name : "Weather Station").toUpperCase(); font.pixelSize: Style.font.body }
            }

            Row {
              visible: !!root.station
              spacing: Style.space(2)
              Text {
                id: bigTemp
                textFormat: Text.PlainText
                text: root.station ? root.fmt(root.station.tempF, 1) : ""
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: 56
                font.bold: true
              }
              Text {
                text: "°F"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
                anchors.top: bigTemp.top
                anchors.topMargin: Style.space(10)
              }
            }

            Grid {
              visible: !!root.station
              columns: 3
              columnSpacing: Style.space(28)
              rowSpacing: Style.space(14)

              Stat { title: "FEELS"; value: root.station ? root.fmt(root.station.feelsLikeF, 0, "°") : "" }
              Stat { title: "HUMID"; value: root.station ? root.fmt(root.station.humidity, 0, "%") : "" }
              Stat { title: "DEW PT"; value: root.station ? root.fmt(root.station.dewPointF, 0, "°") : "" }
              Stat {
                title: "WIND"
                value: root.station ? root.compass(root.station.windDir) + " " + root.fmt(root.station.windMph, 0, " mph") : ""
              }
              Stat { title: "GUST"; value: root.station ? root.fmt(root.station.gustMph, 0, " mph") : "" }
              Stat { title: "PRESSURE"; value: root.station ? root.fmt(root.station.pressureInHg, 2, " in") : "" }
              Stat { title: "RAIN TODAY"; value: root.station ? root.fmt(root.station.rainDailyIn, 2, " in") : "" }
              Stat { title: "UV"; value: root.station ? root.fmt(root.station.uv, 0) : "" }
              Stat { title: "SOLAR"; value: root.station ? root.fmt(root.station.solarWm2, 0, " W/m²") : "" }
              Stat {
                visible: !!root.station && root.station.indoorTempF !== null
                title: "INDOOR"
                value: root.station ? root.fmt(root.station.indoorTempF, 0, "°") + " · " + root.fmt(root.station.indoorHumidity, 0, "%") : ""
              }
            }

            Caption {
              width: parent.width
              wrapMode: Text.WordWrap
              font.italic: true
              text: root.stationError !== "" ? root.stationError
                : (root.station ? root.observedText() : (root.loading ? "Reading station…" : ""))
            }

            Button {
              visible: root.needsSetup
              text: "Set up station"
              iconText: "\uf013" // nf-fa-cog
              bordered: true
              foreground: root.fg
              fontFamily: root.fontFamily
              onClicked: root.openSettings()
            }
          }

          // Divider between the two halves.
          Rectangle {
            x: scroll.width * 0.48
            width: Style.spacing.hairline
            height: scroll.contentHeight
            color: root.fg
            opacity: 0.12
          }

          // ================= Right: forecast =================
          Column {
            id: forecastColumn
            x: scroll.width * 0.5
            width: scroll.width * 0.5 - Style.space(48)
            spacing: Style.space(10)

            Row {
              spacing: Style.space(6)
              Caption { text: "\uf041"; font.pixelSize: Style.font.body } // nf-fa-map_marker
              Caption { text: (root.forecastPlace !== "" ? root.forecastPlace + " forecast" : "Forecast").toUpperCase(); font.pixelSize: Style.font.body }
            }

            Repeater {
              model: root.periods

              Item {
                required property var modelData
                width: forecastColumn.width
                height: Math.max(periodIcon.height, periodText.implicitHeight)

                Text {
                  id: periodIcon
                  width: Style.space(34)
                  textFormat: Text.PlainText
                  text: modelData.icon
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                  anchors.verticalCenter: parent.verticalCenter
                }

                Column {
                  id: periodText
                  anchors.left: periodIcon.right
                  anchors.leftMargin: Style.space(8)
                  anchors.right: tempText.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(1)

                  Caption { text: modelData.name.toUpperCase(); font.pixelSize: Style.font.caption }
                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: modelData.short + (modelData.precip > 0 ? "  ·   " + modelData.precip + "%" : "")
                    wrapMode: Text.WordWrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    color: root.fg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }
                }

                Text {
                  id: tempText
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: modelData.temp + "°"
                  color: modelData.isDaytime ? root.fg : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.heading
                  font.bold: modelData.isDaytime
                }
              }
            }

            Caption {
              visible: root.forecastError !== "" || root.periods.length === 0
              width: parent.width
              wrapMode: Text.WordWrap
              font.italic: true
              text: root.forecastError !== "" ? root.forecastError : "Fetching forecast…"
            }
          }
        }

        // ================= Settings =================
        FocusScope {
          id: settingsScope
          visible: root.showSettings
          x: Style.space(16)
          width: scroll.width - Style.space(32)
          height: settingsForm.implicitHeight
          Keys.onEscapePressed: function(event) { root.closeSettings(); event.accepted = true }

          Column {
            id: settingsForm
            width: parent.width
            spacing: Style.space(14)

            Row {
              spacing: Style.space(6)
              Caption { text: "\uf013"; font.pixelSize: Style.font.body } // nf-fa-cog
              Caption { text: "STATION SETTINGS"; font.pixelSize: Style.font.body }
            }

            Text {
              width: parent.width - Style.space(40)
              wrapMode: Text.WordWrap
              textFormat: Text.StyledText
              color: root.dim
              linkColor: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              text: "Create both keys at <a href=\"https://ambientweather.net/account\">ambientweather.net/account</a>. "
                + "They're saved only in this plugin's config.json, readable by you alone."
              onLinkActivated: function(link) { Qt.openUrlExternally(link) }
              HoverHandler { cursorShape: parent.hoveredLink !== "" ? Qt.PointingHandCursor : Qt.ArrowCursor }
            }

            Grid {
              width: parent.width
              columns: 2
              columnSpacing: Style.space(20)
              rowSpacing: Style.space(12)

              Field {
                id: appKeyField
                width: (parent.width - parent.columnSpacing) / 2
                label: "APPLICATION KEY"
                password: true
                placeholderText: root.savedConfig && root.savedConfig.hasApplicationKey ? "Saved; leave blank to keep" : "Required"
                onAccepted: root.saveSettings()
              }
              Field {
                id: apiKeyField
                width: (parent.width - parent.columnSpacing) / 2
                label: "API KEY"
                password: true
                placeholderText: root.savedConfig && root.savedConfig.hasApiKey ? "Saved; leave blank to keep" : "Required"
                onAccepted: root.saveSettings()
              }
              Field {
                id: macField
                width: (parent.width - parent.columnSpacing) / 2
                label: "STATION MAC (OPTIONAL)"
                placeholderText: "AA:BB:CC:DD:EE:FF"
                hint: "Picks a station when your account has more than one."
                onAccepted: root.saveSettings()
              }
              Field {
                id: ipField
                width: (parent.width - parent.columnSpacing) / 2
                label: "STATION IP (OPTIONAL)"
                placeholderText: "192.168.1.50"
                hint: "With the MAC blank, the MAC is read from the console on save."
                onAccepted: root.saveSettings()
              }
            }

            Item {
              width: parent.width
              height: locationField.height

              Field {
                id: locationField
                width: parent.width - searchButton.width - Style.space(10)
                label: "FORECAST LOCATION"
                placeholderText: "Search for a US city, then pick a result"
                onAccepted: root.searchLocation()
              }
              Button {
                id: searchButton
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.bottomMargin: locationField.hint !== "" ? Style.space(20) : 0
                text: "Search"
                iconText: "\uf002" // nf-fa-search
                bordered: true
                focusable: true
                foreground: root.fg
                fontFamily: root.fontFamily
                onClicked: root.searchLocation()
              }
            }

            Column {
              visible: root.locationResults.length > 0
              width: parent.width
              spacing: Style.space(2)
              Repeater {
                model: root.locationResults
                Button {
                  required property var modelData
                  width: parent.width
                  leftAlign: true
                  focusable: true
                  text: modelData.name + "   " + modelData.latitude + ", " + modelData.longitude
                  iconText: "\uf041" // nf-fa-map_marker
                  foreground: root.fg
                  fontFamily: root.fontFamily
                  onClicked: root.pickLocation(modelData)
                }
              }
            }

            Grid {
              width: parent.width
              columns: 2
              columnSpacing: Style.space(20)
              Field {
                id: latField
                width: (parent.width - parent.columnSpacing) / 2
                label: "LATITUDE"
                placeholderText: "36.0081"
                onAccepted: root.saveSettings()
              }
              Field {
                id: lonField
                width: (parent.width - parent.columnSpacing) / 2
                label: "LONGITUDE"
                placeholderText: "-93.1866"
                onAccepted: root.saveSettings()
              }
            }

            Caption {
              width: parent.width
              wrapMode: Text.WordWrap
              font.letterSpacing: 0
              font.italic: true
              text: "Leave the location blank to use Omarchy's weather location, or else the coordinates saved on your Ambient station. "
                + "The forecast is from the US National Weather Service."
            }

            Row {
              spacing: Style.space(10)
              Button {
                text: root.saving ? "Saving…" : "Save"
                iconText: "\uf00c" // nf-fa-check
                bordered: true
                focusable: true
                enabled: !root.saving
                foreground: root.fg
                fontFamily: root.fontFamily
                onClicked: root.saveSettings()
              }
              Button {
                text: "Cancel"
                bordered: true
                focusable: true
                foreground: root.fg
                fontFamily: root.fontFamily
                onClicked: root.closeSettings()
              }
              Caption {
                anchors.verticalCenter: parent.verticalCenter
                text: root.settingsMessage
                color: root.settingsError ? (root.bar && root.bar.urgent ? root.bar.urgent : Color.foreground) : root.dim
                font.letterSpacing: 0
              }
            }
          }
        }
      }
    }
  }
}
