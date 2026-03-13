Add-Type -AssemblyName System.Drawing

[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$ErrorActionPreference = "Stop"

$scriptRoot = if ($PSScriptRoot) {
    $PSScriptRoot
}
elseif ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
else {
    Join-Path (Get-Location).Path "scripts"
}

function Get-WorldPoint {
    param(
        [double]$Lat,
        [double]$Lon,
        [int]$Zoom
    )

    $scale = 256 * [math]::Pow(2, $Zoom)
    $x = ($Lon + 180.0) / 360.0 * $scale
    $latRad = $Lat * [math]::PI / 180.0
    $y = (1.0 - [math]::Log([math]::Tan($latRad) + (1.0 / [math]::Cos($latRad))) / [math]::PI) / 2.0 * $scale
    [PSCustomObject]@{ X = $x; Y = $y }
}

function New-RoundedRectPath {
    param(
        [float]$X,
        [float]$Y,
        [float]$Width,
        [float]$Height,
        [float]$Radius
    )

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $Radius * 2
    $path.AddArc($X, $Y, $d, $d, 180, 90)
    $path.AddArc($X + $Width - $d, $Y, $d, $d, 270, 90)
    $path.AddArc($X + $Width - $d, $Y + $Height - $d, $d, $d, 0, 90)
    $path.AddArc($X, $Y + $Height - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    return $path
}

function New-MapCanvas {
    param(
        [double]$West,
        [double]$South,
        [double]$East,
        [double]$North,
        [int]$Zoom
    )

    $nw = Get-WorldPoint -Lat $North -Lon $West -Zoom $Zoom
    $se = Get-WorldPoint -Lat $South -Lon $East -Zoom $Zoom
    $width = [int][math]::Ceiling($se.X - $nw.X)
    $height = [int][math]::Ceiling($se.Y - $nw.Y)

    $minTileX = [int][math]::Floor($nw.X / 256.0)
    $maxTileX = [int][math]::Floor(($se.X - 1) / 256.0)
    $minTileY = [int][math]::Floor($nw.Y / 256.0)
    $maxTileY = [int][math]::Floor(($se.Y - 1) / 256.0)

    $bitmap = New-Object System.Drawing.Bitmap($width, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $graphics.Clear([System.Drawing.Color]::White)

    for ($tx = $minTileX; $tx -le $maxTileX; $tx++) {
        for ($ty = $minTileY; $ty -le $maxTileY; $ty++) {
            $tilePath = Join-Path $env:TEMP ("carto-light-" + $Zoom + "-" + $tx + "-" + $ty + ".png")
            if (-not (Test-Path $tilePath)) {
                $tileUrl = "https://basemaps.cartocdn.com/light_nolabels/$Zoom/$tx/$ty.png"
                Invoke-WebRequest -Uri $tileUrl -OutFile $tilePath | Out-Null
            }

            $tile = [System.Drawing.Image]::FromFile($tilePath)
            try {
                $destX = [int][math]::Round($tx * 256.0 - $nw.X)
                $destY = [int][math]::Round($ty * 256.0 - $nw.Y)
                $graphics.DrawImage($tile, $destX, $destY, 256, 256)
            }
            finally {
                $tile.Dispose()
            }
        }
    }

    $wash = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(72, 255, 255, 255))
    $graphics.FillRectangle($wash, 0, 0, $width, $height)
    $wash.Dispose()

    [PSCustomObject]@{
        Bitmap = $bitmap
        Graphics = $graphics
        Zoom = $Zoom
        WorldNorthWest = $nw
    }
}

function Get-CanvasPoint {
    param(
        [object]$Canvas,
        [double]$Lat,
        [double]$Lon
    )

    $pt = Get-WorldPoint -Lat $Lat -Lon $Lon -Zoom $Canvas.Zoom
    [System.Drawing.PointF]::new([float]($pt.X - $Canvas.WorldNorthWest.X), [float]($pt.Y - $Canvas.WorldNorthWest.Y))
}

function Draw-RectLabel {
    param(
        [System.Drawing.Graphics]$Graphics,
        [string]$Text,
        [System.Drawing.Font]$Font,
        [System.Drawing.Color]$FillColor,
        [System.Drawing.Color]$TextColor,
        [float]$X,
        [float]$Y,
        [float]$PaddingX = 10,
        [float]$PaddingY = 6
    )

    $size = $Graphics.MeasureString($Text, $Font)
    $rect = [System.Drawing.RectangleF]::new($X, $Y, $size.Width + $PaddingX * 2, $size.Height + $PaddingY * 2)
    $path = New-RoundedRectPath -X $rect.X -Y $rect.Y -Width $rect.Width -Height $rect.Height -Radius 8
    $fill = New-Object System.Drawing.SolidBrush($FillColor)
    $textBrush = New-Object System.Drawing.SolidBrush($TextColor)
    $Graphics.FillPath($fill, $path)
    $Graphics.DrawString($Text, $Font, $textBrush, $X + $PaddingX, $Y + $PaddingY)
    $textBrush.Dispose()
    $fill.Dispose()
    $path.Dispose()
}

function Draw-Callout {
    param(
        [System.Drawing.Graphics]$Graphics,
        [string]$Text,
        [System.Drawing.Font]$Font,
        [System.Drawing.PointF]$Anchor,
        [float]$LabelX,
        [float]$LabelY,
        [System.Drawing.Color]$LineColor
    )

    $paddingX = 10
    $paddingY = 6
    $size = $Graphics.MeasureString($Text, $Font)
    $rect = [System.Drawing.RectangleF]::new(
        $LabelX,
        $LabelY,
        $size.Width + $paddingX * 2,
        $size.Height + $paddingY * 2
    )

    $linePen = New-Object System.Drawing.Pen($LineColor, 2.4)
    $linePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round

    if ($Anchor.Y -lt $rect.Top) {
        $edgeX = [math]::Max($rect.Left + 8, [math]::Min($Anchor.X, $rect.Right - 8))
        $edgeY = $rect.Top
    }
    elseif ($Anchor.Y -gt $rect.Bottom) {
        $edgeX = [math]::Max($rect.Left + 8, [math]::Min($Anchor.X, $rect.Right - 8))
        $edgeY = $rect.Bottom
    }
    elseif ($Anchor.X -lt $rect.Left) {
        $edgeX = $rect.Left
        $edgeY = [math]::Max($rect.Top + 6, [math]::Min($Anchor.Y, $rect.Bottom - 6))
    }
    else {
        $edgeX = $rect.Right
        $edgeY = [math]::Max($rect.Top + 6, [math]::Min($Anchor.Y, $rect.Bottom - 6))
    }

    $lineStart = [System.Drawing.PointF]::new([float]$edgeX, [float]$edgeY)
    $Graphics.DrawLine($linePen, $lineStart, $Anchor)
    $dotBrush = New-Object System.Drawing.SolidBrush($LineColor)
    $Graphics.FillEllipse($dotBrush, $Anchor.X - 4, $Anchor.Y - 4, 8, 8)

    Draw-RectLabel -Graphics $Graphics -Text $Text -Font $Font `
        -FillColor ([System.Drawing.Color]::FromArgb(232, 255, 255, 255)) `
        -TextColor ([System.Drawing.Color]::FromArgb(28, 37, 54)) `
        -X $LabelX -Y $LabelY

    $dotBrush.Dispose()
    $linePen.Dispose()
}

function Save-Png {
    param(
        [System.Drawing.Bitmap]$Bitmap,
        [string]$Path
    )

    $dir = Split-Path $Path -Parent
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    $Bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
}

$titleFont = New-Object System.Drawing.Font("Microsoft YaHei", 26, [System.Drawing.FontStyle]::Bold)
$subtitleFont = New-Object System.Drawing.Font("Microsoft YaHei", 11, [System.Drawing.FontStyle]::Regular)
$labelFont = New-Object System.Drawing.Font("Microsoft YaHei", 12, [System.Drawing.FontStyle]::Bold)
$smallFont = New-Object System.Drawing.Font("Microsoft YaHei", 10, [System.Drawing.FontStyle]::Regular)
$tinyFont = New-Object System.Drawing.Font("Microsoft YaHei", 9, [System.Drawing.FontStyle]::Regular)

$deepBlue = [System.Drawing.Color]::FromArgb(25, 55, 109)
$riverBlue = [System.Drawing.Color]::FromArgb(47, 116, 181)
$routeBlue = [System.Drawing.Color]::FromArgb(34, 133, 204)
$redBrown = [System.Drawing.Color]::FromArgb(153, 65, 39)
$sand = [System.Drawing.Color]::FromArgb(110, 205, 166, 102)
$darkText = [System.Drawing.Color]::FromArgb(34, 34, 34)

$overview = New-MapCanvas -West 118.60 -South 31.99 -East 119.22 -North 32.30 -Zoom 12
try {
    $g = $overview.Graphics
    $bmp = $overview.Bitmap

    Draw-RectLabel -Graphics $g -Text "黄天荡区位图" -Font $titleFont `
        -FillColor ([System.Drawing.Color]::FromArgb(228, 255, 255, 255)) `
        -TextColor $darkText -X 22 -Y 18 -PaddingX 14 -PaddingY 8
    Draw-RectLabel -Graphics $g -Text "先交代它在今南京附近哪里，再进入战局" -Font $subtitleFont `
        -FillColor ([System.Drawing.Color]::FromArgb(210, 255, 255, 255)) `
        -TextColor ([System.Drawing.Color]::FromArgb(90, 90, 90)) -X 26 -Y 70 -PaddingX 10 -PaddingY 5

    $nanjing = Get-CanvasPoint -Canvas $overview -Lat 32.0438284 -Lon 118.7788631
    $longtan = Get-CanvasPoint -Canvas $overview -Lat 32.1726768 -Lon 119.0529004
    $huangtiandang = Get-CanvasPoint -Canvas $overview -Lat 32.2384831 -Lon 119.1191044

    $ringPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(180, 181, 50, 45), 5)
    $ringPen.Alignment = [System.Drawing.Drawing2D.PenAlignment]::Center
    $g.DrawEllipse($ringPen, $huangtiandang.X - 18, $huangtiandang.Y - 18, 36, 36)
    $ringPen.Dispose()

    $fillBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(160, 181, 50, 45))
    $g.FillEllipse($fillBrush, $huangtiandang.X - 6, $huangtiandang.Y - 6, 12, 12)
    $g.FillEllipse($fillBrush, $nanjing.X - 5, $nanjing.Y - 5, 10, 10)
    $fillBrush.Dispose()

    Draw-Callout -Graphics $g -Text "黄天荡（今龙潭街道一带）" -Font $labelFont `
        -Anchor $huangtiandang -LabelX ($huangtiandang.X - 255) -LabelY ($huangtiandang.Y + 16) `
        -LineColor ([System.Drawing.Color]::FromArgb(181, 50, 45))

    Draw-Callout -Graphics $g -Text "南京" -Font $labelFont `
        -Anchor $nanjing -LabelX ($nanjing.X - 14) -LabelY ($nanjing.Y + 16) `
        -LineColor ([System.Drawing.Color]::FromArgb(75, 75, 75))

    Draw-Callout -Graphics $g -Text "龙潭" -Font $smallFont `
        -Anchor $longtan -LabelX ($longtan.X - 10) -LabelY ($longtan.Y + 10) `
        -LineColor ([System.Drawing.Color]::FromArgb(110, 110, 110))

    Draw-RectLabel -Graphics $g -Text "长江" -Font $labelFont `
        -FillColor ([System.Drawing.Color]::FromArgb(200, 230, 241, 255)) `
        -TextColor $deepBlue -X ($bmp.Width - 146) -Y 54 -PaddingX 14 -PaddingY 7

    $note = "黄天荡原有水荡今已大体消失，图中位置按现存黄天荡古战场遗址 / 湿地公园一带近似标示。"
    Draw-RectLabel -Graphics $g -Text $note -Font $tinyFont `
        -FillColor ([System.Drawing.Color]::FromArgb(208, 255, 255, 255)) `
        -TextColor ([System.Drawing.Color]::FromArgb(80, 80, 80)) -X 24 -Y ($bmp.Height - 42) -PaddingX 10 -PaddingY 5

    Save-Png -Bitmap $bmp -Path ([System.IO.Path]::GetFullPath((Join-Path $scriptRoot "..\\photobed\\nansong-1\\huangtiandang-overview-map.png")))
}
finally {
    $overview.Graphics.Dispose()
    $overview.Bitmap.Dispose()
}

$detail = New-MapCanvas -West 118.97 -South 32.11 -East 119.21 -North 32.29 -Zoom 13
try {
    $g = $detail.Graphics
    $bmp = $detail.Bitmap

    Draw-RectLabel -Graphics $g -Text "黄天荡之战水域态势图" -Font $titleFont `
        -FillColor ([System.Drawing.Color]::FromArgb(228, 255, 255, 255)) `
        -TextColor $darkText -X 22 -Y 18 -PaddingX 14 -PaddingY 8
    Draw-RectLabel -Graphics $g -Text "韩世忠封住北渡口，金军只能凿老鹳河故道绕出上游" -Font $subtitleFont `
        -FillColor ([System.Drawing.Color]::FromArgb(210, 255, 255, 255)) `
        -TextColor ([System.Drawing.Color]::FromArgb(90, 90, 90)) -X 26 -Y 70 -PaddingX 10 -PaddingY 5

    $huang = Get-CanvasPoint -Canvas $detail -Lat 32.2384831 -Lon 119.1191044
    $longtan = Get-CanvasPoint -Canvas $detail -Lat 32.1726768 -Lon 119.0529004
    $xiashu = Get-CanvasPoint -Canvas $detail -Lat 32.1725107 -Lon 119.1629657

    $blockedArea = @(
        (Get-CanvasPoint -Canvas $detail -Lat 32.245 -Lon 119.084),
        (Get-CanvasPoint -Canvas $detail -Lat 32.249 -Lon 119.145),
        (Get-CanvasPoint -Canvas $detail -Lat 32.214 -Lon 119.152),
        (Get-CanvasPoint -Canvas $detail -Lat 32.193 -Lon 119.127),
        (Get-CanvasPoint -Canvas $detail -Lat 32.196 -Lon 119.072),
        (Get-CanvasPoint -Canvas $detail -Lat 32.224 -Lon 119.060)
    )
    $areaBrush = New-Object System.Drawing.SolidBrush($sand)
    $areaPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(178, 157, 92, 31), 2)
    $g.FillPolygon($areaBrush, $blockedArea)
    $g.DrawPolygon($areaPen, $blockedArea)
    $areaBrush.Dispose()
    $areaPen.Dispose()

    $blockNorth = Get-CanvasPoint -Canvas $detail -Lat 32.248 -Lon 119.060
    $blockSouth = Get-CanvasPoint -Canvas $detail -Lat 32.206 -Lon 119.060
    $blockPen = New-Object System.Drawing.Pen($deepBlue, 7)
    $blockPen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $blockPen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $g.DrawLine($blockPen, $blockNorth, $blockSouth)
    $blockPen.Dispose()

    $shipBrush = New-Object System.Drawing.SolidBrush($deepBlue)
    foreach ($offset in @(0, 18, 36, 54)) {
        $g.FillRectangle($shipBrush, $blockNorth.X - 10, $blockNorth.Y + $offset, 20, 6)
    }
    $shipBrush.Dispose()

    Draw-RectLabel -Graphics $g -Text "韩世忠水军封锁线" -Font $labelFont `
        -FillColor ([System.Drawing.Color]::FromArgb(220, 237, 245, 255)) `
        -TextColor $deepBlue -X ($blockNorth.X - 112) -Y ($blockSouth.Y - 6) -PaddingX 10 -PaddingY 6
    Draw-RectLabel -Graphics $g -Text "48天" -Font $labelFont `
        -FillColor ([System.Drawing.Color]::FromArgb(230, 25, 55, 109)) `
        -TextColor ([System.Drawing.Color]::White) -X ($blockNorth.X + 18) -Y ($blockNorth.Y + 26) -PaddingX 10 -PaddingY 6

    $routePoints = @(
        (Get-CanvasPoint -Canvas $detail -Lat 32.238 -Lon 119.119),
        (Get-CanvasPoint -Canvas $detail -Lat 32.220 -Lon 119.112),
        (Get-CanvasPoint -Canvas $detail -Lat 32.195 -Lon 119.112),
        (Get-CanvasPoint -Canvas $detail -Lat 32.171 -Lon 119.095),
        (Get-CanvasPoint -Canvas $detail -Lat 32.168 -Lon 119.048),
        (Get-CanvasPoint -Canvas $detail -Lat 32.188 -Lon 119.012),
        (Get-CanvasPoint -Canvas $detail -Lat 32.211 -Lon 119.015)
    )

    $routePen = New-Object System.Drawing.Pen($routeBlue, 4)
    $routePen.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
    $routePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $routePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $g.DrawLines($routePen, $routePoints)
    $routePen.Dispose()

    $arrowPen = New-Object System.Drawing.Pen($redBrown, 4)
    $arrowPen.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
    $arrowPen.CustomEndCap = New-Object System.Drawing.Drawing2D.AdjustableArrowCap(6, 8)
    $g.DrawLines($arrowPen, $routePoints)
    $arrowPen.Dispose()

    Draw-RectLabel -Graphics $g -Text "老鹳河故道（大致走向）" -Font $smallFont `
        -FillColor ([System.Drawing.Color]::FromArgb(222, 240, 249, 255)) `
        -TextColor $routeBlue -X ($routePoints[2].X + 8) -Y ($routePoints[2].Y + 10) -PaddingX 10 -PaddingY 6
    Draw-RectLabel -Graphics $g -Text "凿渠30里" -Font $labelFont `
        -FillColor ([System.Drawing.Color]::FromArgb(228, 153, 65, 39)) `
        -TextColor ([System.Drawing.Color]::White) -X ($routePoints[4].X - 86) -Y ($routePoints[4].Y - 58) -PaddingX 10 -PaddingY 6
    Draw-RectLabel -Graphics $g -Text "金军脱出方向" -Font $smallFont `
        -FillColor ([System.Drawing.Color]::FromArgb(230, 255, 240, 235)) `
        -TextColor $redBrown -X ($routePoints[5].X - 10) -Y ($routePoints[5].Y - 44) -PaddingX 10 -PaddingY 6

    Draw-RectLabel -Graphics $g -Text "金军被困区域" -Font $labelFont `
        -FillColor ([System.Drawing.Color]::FromArgb(224, 255, 247, 231)) `
        -TextColor ([System.Drawing.Color]::FromArgb(122, 74, 20)) -X ($huang.X - 96) -Y ($huang.Y + 42) -PaddingX 10 -PaddingY 6
    Draw-RectLabel -Graphics $g -Text "黄天荡" -Font $labelFont `
        -FillColor ([System.Drawing.Color]::FromArgb(232, 255, 255, 255)) `
        -TextColor $darkText -X ($huang.X - 64) -Y ($huang.Y - 48) -PaddingX 10 -PaddingY 6

    $markBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(181, 50, 45))
    $g.FillEllipse($markBrush, $huang.X - 5, $huang.Y - 5, 10, 10)
    $markBrush.Dispose()

    Draw-RectLabel -Graphics $g -Text "龙潭" -Font $smallFont `
        -FillColor ([System.Drawing.Color]::FromArgb(222, 255, 255, 255)) `
        -TextColor ([System.Drawing.Color]::FromArgb(85, 85, 85)) -X ($longtan.X + 28) -Y ($longtan.Y + 14) -PaddingX 10 -PaddingY 5
    Draw-RectLabel -Graphics $g -Text "下蜀" -Font $smallFont `
        -FillColor ([System.Drawing.Color]::FromArgb(222, 255, 255, 255)) `
        -TextColor ([System.Drawing.Color]::FromArgb(85, 85, 85)) -X ($xiashu.X - 18) -Y ($xiashu.Y - 30) -PaddingX 10 -PaddingY 5
    Draw-RectLabel -Graphics $g -Text "长江" -Font $labelFont `
        -FillColor ([System.Drawing.Color]::FromArgb(200, 230, 241, 255)) `
        -TextColor $deepBlue -X ($bmp.Width - 130) -Y 100 -PaddingX 14 -PaddingY 7

    $detailNote = "基于现代水系骨架的历史复原示意：封锁线、被困区与老鹳河故道均为示意。"
    Draw-RectLabel -Graphics $g -Text $detailNote -Font $tinyFont `
        -FillColor ([System.Drawing.Color]::FromArgb(208, 255, 255, 255)) `
        -TextColor ([System.Drawing.Color]::FromArgb(80, 80, 80)) -X 24 -Y ($bmp.Height - 42) -PaddingX 10 -PaddingY 5

    Save-Png -Bitmap $bmp -Path ([System.IO.Path]::GetFullPath((Join-Path $scriptRoot "..\\photobed\\nansong-1\\huangtiandang-breakout-map.png")))
}
finally {
    $detail.Graphics.Dispose()
    $detail.Bitmap.Dispose()
}

$titleFont.Dispose()
$subtitleFont.Dispose()
$labelFont.Dispose()
$smallFont.Dispose()
$tinyFont.Dispose()

