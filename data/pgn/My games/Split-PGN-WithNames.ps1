param(
    # Перечисли все варианты твоего имени, как они встречаются в PGN
    [string[]]$YourNames = @("Mitusov, Semen","Mitusov Semen","Semen Mitusov","Mitusov")
)

# === Настройки ===
$inputFile    = ".\my games.pgn"
$outputFolder = ".\SplitGames"

# Создадим папку для результатов
New-Item -ItemType Directory -Force -Path $outputFolder | Out-Null

function Sanitize-ForPath([string]$s, [int]$maxLen = 70) {
    if ([string]::IsNullOrWhiteSpace($s)) { return "Unknown" }
    $s = $s -replace '[<>:"/\\|?*]', ' ' -replace '\s+', ' ' -replace '^\s+|\s+$',''
    if ($s.Length -gt $maxLen) { $s = $s.Substring(0, $maxLen).Trim() }
    return $s
}

function Get-TagValue([string]$text, [string]$tag) {
    $m = [regex]::Match($text, "^\[$tag\s+""(.*?)""\]", 'Multiline')
    if ($m.Success) { return $m.Groups[1].Value } else { return "" }
}

# Нормализуем имя: без регистра, пробелов, запятых, точек и дефисов
function Normalize-Name([string]$s) {
    if ([string]::IsNullOrWhiteSpace($s)) { return "" }
    $t = $s.ToLowerInvariant()
    $t = ($t -replace '[\s,.\-]', '')
    return $t
}

# Совпадает ли кандидат с любым из YourNames
function Is-YourName([string]$candidate, [string[]]$yourNames) {
    $normCand = Normalize-Name $candidate
    foreach ($yn in $yourNames) {
        if ($normCand -ne "" -and $normCand -eq (Normalize-Name $yn)) { return $true }
    }
    return $false
}

# Читаем весь PGN и режем по началу партии
$content = Get-Content $inputFile -Raw
$games = ($content -split '(?m)^\[Event ') | Where-Object { $_.Trim() -ne "" } | ForEach-Object { "[Event " + $_ }

$index = 1
foreach ($game in $games) {
    $white  = Get-TagValue $game "White"
    $black  = Get-TagValue $game "Black"
    $date   = Get-TagValue $game "Date"
    $result = Get-TagValue $game "Result"
    $eco    = Get-TagValue $game "ECO"

    # Приведём удобные поля
    $dateClean = ""
    if ($date -match '^\d{4}\.\d{2}\.\d{2}$') { $dateClean = $date }

    $resultClean = $result
    if ($result -eq '1/2-1/2') { $resultClean = 'draw' }
    elseif ($result -eq '*') { $resultClean = 'ongoing' }

    # Определяем соперника
    $youAreWhite = Is-YourName $white $YourNames
    $youAreBlack = Is-YourName $black $YourNames
    $opponent = ""
    $myName = ""

    if ($youAreWhite) {
        $opponent = $black
        $myName = $white
    } elseif ($youAreBlack) {
        $opponent = $white
        $myName = $black
    }

    # Формируем имя файла
    $whoPart = ""
    if ($opponent -ne "") {
        $whoPart = "{0} vs {1}" -f (Sanitize-ForPath $myName), (Sanitize-ForPath $opponent)
    } else {
        # Фоллбэк, если твоё имя не найдено
        $whoPart = "{0} vs {1}" -f (Sanitize-ForPath $white), (Sanitize-ForPath $black)
    }

    $parts = @()
    $parts += $whoPart
    if ($dateClean -ne "") { $parts += $dateClean }
    if ($eco -ne "")       { $parts += ("ECO " + (Sanitize-ForPath $eco, 10)) }
    if ($resultClean -ne "") { $parts += $resultClean }

    $baseName = ($parts -join " - ")
    $fileName = "{0:D3} - {1}.pgn" -f $index, (Sanitize-ForPath $baseName)

    # Запишем БЕЗ изменений содержимое партии
    $outPath = Join-Path $outputFolder $fileName
    $game.TrimEnd() + "`r`n" | Out-File -Encoding UTF8 -FilePath $outPath

    $index++
}

# Сообщение об окончании (ASCII, чтобы точно не было проблем с кодировкой в WinPS 5.1)
Write-Host ("Done! Exported {0} games to folder: {1}" -f ($index-1), $outputFolder)


