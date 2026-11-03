#requires -Version 5.1
<#
.SYNOPSIS
    Sincroniza o Product Backlog do repositorio com GitHub Issues + Projects v2.

.DESCRIPTION
    Le .github/backlog/hus.json e garante, de forma idempotente:
      - labels do repositorio;
      - milestone do MVP;
      - 13 HUs + 3 epicos como Issues (marcador <!-- hu-id: HUxx -->);
      - Project v2 com campos e itens preenchidos;
      - indice .github/backlog/INDEX.md (com -ExportIndex).

    Sem -Apply o script apenas calcula e mostra o plano (dry-run).
    Leituras (gh list/api GET) ocorrem tambem no dry-run; escritas nao.

.PARAMETER Apply
    Executa as escritas no GitHub. Sem este flag nada e alterado.

.PARAMETER Scope
    all (padrao), labels, issues ou project.

.PARAMETER UpdateExisting
    Reescreve titulo e corpo de issues ja existentes. Use quando o JSON mudar.
    Sem este flag, issues existentes sao preservadas (protege edicoes manuais).

.PARAMETER ExportIndex
    Regenera .github/backlog/INDEX.md a partir das issues existentes.

.PARAMETER DataFile
    Caminho alternativo para o JSON do backlog.

.EXAMPLE
    .\scripts\sync-backlog.ps1

.EXAMPLE
    .\scripts\sync-backlog.ps1 -Apply -ExportIndex
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [ValidateSet('all', 'labels', 'issues', 'project')]
    [string]$Scope = 'all',
    [switch]$UpdateExisting,
    [switch]$ExportIndex,
    [switch]$ConfigureStatus,
    [int]$ProjectNumber,
    [string]$ProjectTitle,
    [string]$ProjectOwner = '@me',
    [string]$Owner,
    [string]$Repo,
    [string]$DataFile
)

$ErrorActionPreference = 'Stop'

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

$Root = Split-Path -Parent $PSScriptRoot
if (-not $DataFile) { $DataFile = Join-Path $Root '.github\backlog\hus.json' }
if (-not (Test-Path $DataFile)) { throw "Arquivo de dados não encontrado: $DataFile" }

$Data = Get-Content -Path $DataFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Owner) { $Owner = $Data.owner }
if (-not $Repo) { $Repo = $Data.repo }
if (-not $ProjectTitle) { $ProjectTitle = $Data.project.title }
$FullRepo = "$Owner/$Repo"
$Milestone = $Data.project.milestone

function Write-Step { param([string]$Message) Write-Host "[ETAPA] $Message" -ForegroundColor Cyan }
function Write-Plan { param([string]$Message) Write-Host "[PLANO] $Message" }
function Write-Ok { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Skip { param([string]$Message) Write-Host "[PULAR] $Message" -ForegroundColor DarkGray }
function Write-Warn2 { param([string]$Message) Write-Host "[AVISO] $Message" -ForegroundColor Yellow }

function Invoke-Gh {
    param([string[]]$Arguments, [switch]$Raw)
    $attempt = 0
    while ($true) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $output = & gh @Arguments 2>&1
        $code = $LASTEXITCODE
        $ErrorActionPreference = $previous
        if ($code -eq 0) { break }
        $text = ($output | Out-String)
        if ($attempt -lt 5 -and $text -match 'rate limit') {
            $attempt++
            $wait = 30 * $attempt
            Write-Host "[AGUARDANDO] rate limit do GitHub; nova tentativa em ${wait}s" -ForegroundColor Yellow
            Start-Sleep -Seconds $wait
            continue
        }
        throw ("gh {0} falhou:`n{1}" -f ($Arguments -join ' '), ($output -join "`n"))
    }
    if ($Raw) { return (($output | Out-String).Trim()) }
    return $output
}

function New-TempBody {
    param([string]$Content)
    $path = Join-Path $env:TEMP ("backlog-" + [guid]::NewGuid().ToString('N') + ".md")
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $Content, $encoding)
    return $path
}

function Invoke-Graphql {
    param([string]$Query, [hashtable]$Variables)
    $body = @{ query = $Query }
    if ($Variables) { $body.variables = $Variables }
    $json = $body | ConvertTo-Json -Compress -Depth 8
    $path = Join-Path $env:TEMP ("graphql-" + [guid]::NewGuid().ToString('N') + ".json")
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
    try {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $out = & gh api graphql --input $path 2>&1
        $code = $LASTEXITCODE
        $ErrorActionPreference = $previous
        if ($code -ne 0) { throw ("GraphQL falhou: " + ($out -join "`n")) }
        return ((($out | Out-String).Trim()) | ConvertFrom-Json)
    }
    finally { Remove-Item -Path $path -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- pre-requisitos

$authText = (& gh auth status 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'gh não está autenticado. Rode: gh auth login' }
$script:HasProjectScope = ($authText -match "'project'")
$needsProjectScope = ($Scope -eq 'all' -or $Scope -eq 'project')
if ($needsProjectScope -and -not $script:HasProjectScope) {
    throw "Token sem escopo 'project'. Rode: gh auth refresh -h github.com -s project"
}

# ---------------------------------------------------------------- labels

function Sync-Labels {
    Write-Step 'Labels'
    $raw = Invoke-Gh -Raw -Arguments @('label', 'list', '--repo', $FullRepo, '--limit', '200', '--json', 'name')
    $existing = @{}
    foreach ($item in ($raw | ConvertFrom-Json)) { $existing[$item.name] = $true }

    foreach ($label in $Data.labels) {
        if ($existing.ContainsKey($label.name)) {
            Write-Skip "label $($label.name) ja existe"
            continue
        }
        if ($Apply) {
            Invoke-Gh -Arguments @('label', 'create', $label.name, '--repo', $FullRepo,
                '--color', $label.color, '--description', $label.description) | Out-Null
            Write-Ok "label $($label.name) criada"
        }
        else {
            Write-Plan "criar label $($label.name) (#$($label.color))"
        }
    }
}

# ---------------------------------------------------------------- milestone

function Sync-Milestone {
    $raw = Invoke-Gh -Raw -Arguments @('api', "repos/$FullRepo/milestones?state=all", '--jq', '.[].title')
    $titles = @($raw -split "`n" | Where-Object { $_ })
    if ($titles -contains $Milestone) {
        Write-Skip "milestone $Milestone ja existe"
        return
    }
    if ($Apply) {
        Invoke-Gh -Arguments @('api', '-X', 'POST', "repos/$FullRepo/milestones",
            '-f', "title=$Milestone",
            '-f', 'description=Escopo mínimo viável do sistema de monitoramento de preços, estoque e compras') | Out-Null
        Write-Ok "milestone $Milestone criada"
    }
    else {
        Write-Plan "criar milestone $Milestone"
    }
}

# ---------------------------------------------------------------- issues

function Get-Issues {
    $raw = Invoke-Gh -Raw -Arguments @('issue', 'list', '--repo', $FullRepo, '--state', 'all',
        '--limit', '200', '--json', 'number,title,body,state')
    if (-not $raw) { return @() }
    return @($raw | ConvertFrom-Json)
}

function Get-IssueMarker {
    param([string]$Body)
    if ($Body -and $Body -match '<!--\s*hu-id:\s*(?<id>[A-Za-z0-9]+)\s*-->') { return $Matches['id'] }
    return $null
}

function New-HuBody {
    param($Hu, [int]$EpicNumber)
    $epic = $Data.epics | Where-Object { $_.id -eq $Hu.epic } | Select-Object -First 1
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("<!-- hu-id: $($Hu.id) -->")
    $lines.Add('')
    $lines.Add("**Épico:** #$EpicNumber — $($epic.title)  ")
    $lines.Add("**Product Backlog:** $($Hu.pb)  ")
    $lines.Add("**Branch sugerida:** ``$($Hu.branch)``")
    $lines.Add('')
    $lines.Add('## História')
    $lines.Add('')
    $lines.Add($Hu.story)
    $lines.Add('')
    $lines.Add('## Critérios de aceitação')
    $lines.Add('')
    foreach ($criterion in $Hu.criteria) { $lines.Add("- [ ] $criterion") }
    $lines.Add('')
    $lines.Add('## Item do Product Backlog')
    $lines.Add('')
    $lines.Add($Hu.pbDescription)
    $lines.Add('')
    $lines.Add('## Definição de Pronto')
    $lines.Add('')
    foreach ($done in $Data.definitionOfDone) { $lines.Add("- [ ] $done") }
    $lines.Add('')
    $lines.Add('## Rastreabilidade')
    $lines.Add('')
    $lines.Add("Branch ``$($Hu.branch)`` — o PR deve conter ``Closes #<numero desta issue>``")
    return ($lines -join "`n")
}

function New-EpicBody {
    param($Epic, [array]$Children)
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("<!-- hu-id: $($Epic.id) -->")
    $lines.Add('')
    $lines.Add($Epic.description)
    $lines.Add('')
    $lines.Add('## Histórias de usuário')
    $lines.Add('')
    if ($Children -and $Children.Count -gt 0) {
        foreach ($child in $Children) {
            $lines.Add("- [ ] #$($child.number) — $($child.title)")
        }
        $lines.Add('')
        $lines.Add('A barra de progresso deste épico avança conforme as issues filhas são fechadas.')
    }
    else {
        $lines.Add('_Histórias ainda não vinculadas._')
    }
    return ($lines -join "`n")
}

function New-Issue {
    param([string]$Title, [string]$Body, [string[]]$Labels, [string]$MilestoneName)
    $tmp = New-TempBody -Content $Body
    try {
        $arguments = @('issue', 'create', '--repo', $FullRepo, '--title', $Title,
            '--body-file', $tmp, '--milestone', $MilestoneName)
        foreach ($label in $Labels) { $arguments += @('--label', $label) }
        $url = (Invoke-Gh -Raw -Arguments $arguments)
        if ($url -match '/issues/(\d+)') { return [int]$Matches[1] }
        throw "Não foi possível identificar a issue criada: $url"
    }
    finally {
        Remove-Item -Path $tmp -ErrorAction SilentlyContinue
    }
}

function Update-Issue {
    param([int]$Number, [string]$Title, [string]$Body, [string[]]$Labels, [string]$MilestoneName)
    $tmp = New-TempBody -Content $Body
    try {
        $arguments = @('issue', 'edit', "$Number", '--repo', $FullRepo, '--title', $Title,
            '--body-file', $tmp, '--milestone', $MilestoneName)
        foreach ($label in $Labels) { $arguments += @('--add-label', $label) }
        Invoke-Gh -Arguments $arguments | Out-Null
    }
    finally {
        Remove-Item -Path $tmp -ErrorAction SilentlyContinue
    }
}

function Sync-Issues {
    Write-Step "Issues (13 HUs + 3 épicos)"

    $issues = Get-Issues
    $byMarker = @{}
    foreach ($issue in $issues) {
        $marker = Get-IssueMarker -Body $issue.body
        if ($marker) { $byMarker[$marker] = $issue }
    }

    # Fase 1 — épicos (corpo sem filhos; a lista é vinculada na fase 3)
    $epicNumbers = @{}
    foreach ($epic in $Data.epics) {
        $labels = @('tipo:epico', $epic.label, 'prioridade:alta')
        if ($byMarker.ContainsKey($epic.id)) {
            $epicNumbers[$epic.id] = [int]$byMarker[$epic.id].number
            Write-Skip "épico $($epic.id) já existe (#$($epicNumbers[$epic.id]))"
            continue
        }
        $body = New-EpicBody -Epic $epic -Children @()
        if ($Apply) {
            $number = New-Issue -Title $epic.title -Body $body -Labels $labels -MilestoneName $Milestone
            $epicNumbers[$epic.id] = $number
            $byMarker[$epic.id] = [pscustomobject]@{ number = $number; title = $epic.title; body = $body }
            Write-Ok "Épico $($epic.id) criado (#$number)"
        }
        else {
            $epicNumbers[$epic.id] = 0
            Write-Plan "criar $($epic.id) — $($epic.title)"
        }
    }

    # Fase 2 — histórias de usuário
    $huNumbers = @{}
    foreach ($hu in $Data.hus) {
        $epic = $Data.epics | Where-Object { $_.id -eq $hu.epic } | Select-Object -First 1
        $labels = @('tipo:hu', $epic.label, "prioridade:$($hu.priority.ToLower())")
        $epicNumber = [int]$epicNumbers[$hu.epic]
        $body = New-HuBody -Hu $hu -EpicNumber $epicNumber

        if ($byMarker.ContainsKey($hu.id)) {
            $existing = $byMarker[$hu.id]
            $huNumbers[$hu.id] = [int]$existing.number
            if ($UpdateExisting) {
                if ($Apply) {
                    Update-Issue -Number $existing.number -Title $hu.title -Body $body -Labels $labels -MilestoneName $Milestone
                    Write-Ok "HU $($hu.id) atualizada (#$($existing.number))"
                }
                else { Write-Plan "atualizar HU $($hu.id) (#$($existing.number))" }
            }
            else {
                Write-Skip "HU $($hu.id) já existe (#$($existing.number))"
            }
            continue
        }

        if ($Apply) {
            $number = New-Issue -Title $hu.title -Body $body -Labels $labels -MilestoneName $Milestone
            $huNumbers[$hu.id] = $number
            Write-Ok "HU $($hu.id) criada (#$number)"
        }
        else {
            $huNumbers[$hu.id] = 0
            Write-Plan "criar $($hu.id) — $($hu.title)"
        }
    }

    # Fase 3 — lista de HUs dentro de cada épico
    foreach ($epic in $Data.epics) {
        $children = @()
        foreach ($hu in ($Data.hus | Where-Object { $_.epic -eq $epic.id })) {
            $children += [pscustomobject]@{ number = [int]$huNumbers[$hu.id]; title = $hu.title }
        }
        $number = [int]$epicNumbers[$epic.id]
        if (-not $number) {
            Write-Plan "vincular HUs ao épico $($epic.id)"
            continue
        }

        $current = $byMarker[$epic.id]
        $needsUpdate = $UpdateExisting
        foreach ($child in $children) {
            if ($child.number -gt 0 -and $current.body -notmatch "#$($child.number)\b") { $needsUpdate = $true }
        }
        if (-not $needsUpdate) {
            Write-Skip "épico $($epic.id) já lista as HUs (#$number)"
            continue
        }

        $body = New-EpicBody -Epic $epic -Children $children
        $labels = @('tipo:epico', $epic.label, 'prioridade:alta')
        if ($Apply) {
            Update-Issue -Number $number -Title $epic.title -Body $body -Labels $labels -MilestoneName $Milestone
            Write-Ok "épico $($epic.id) atualizado com a lista de HUs (#$number)"
        }
        else { Write-Plan "atualizar épico $($epic.id) com a lista de HUs (#$number)" }
    }
}

# ---------------------------------------------------------------- project

function Get-Project {
    $raw = Invoke-Gh -Raw -Arguments @('project', 'list', '--owner', $ProjectOwner, '--format', 'json', '--limit', '50')
    $parsed = $raw | ConvertFrom-Json
    if ($ProjectNumber -gt 0) {
        return ($parsed.projects | Where-Object { [int]$_.number -eq $ProjectNumber } | Select-Object -First 1)
    }
    return ($parsed.projects | Where-Object { $_.title -eq $ProjectTitle } | Select-Object -First 1)
}

function Get-ProjectFields {
    param([int]$Number)
    $raw = Invoke-Gh -Raw -Arguments @('project', 'field-list', "$Number", '--owner', $ProjectOwner, '--format', 'json', '--limit', '50')
    return ($raw | ConvertFrom-Json).fields
}

function Get-ProjectItems {
    param([int]$Number)
    $raw = Invoke-Gh -Raw -Arguments @('project', 'item-list', "$Number", '--owner', $ProjectOwner, '--format', 'json', '--limit', '200')
    if (-not $raw) { return @() }
    return @(($raw | ConvertFrom-Json).items)
}

function Sync-Project {
    Write-Step 'Project v2'

    $project = Get-Project
    if (-not $project) {
        if ($ProjectNumber -gt 0) {
            throw "Project #$ProjectNumber não encontrado para $Owner. Confira o número informado."
        }
        if ($Apply) {
            $raw = Invoke-Gh -Raw -Arguments @('project', 'create', '--owner', $ProjectOwner, '--title', $ProjectTitle, '--format', 'json')
            $project = $raw | ConvertFrom-Json
            Write-Ok "project $($project.number) criado"
        }
        else {
            Write-Plan "criar project '$ProjectTitle' e vinculá-lo ao repositório"
            return
        }
    }
    else {
        Write-Skip "project $($project.number) já existe"
    }

    $number = [int]$project.number

    # vinculo com o repositorio (idempotente; ignora erro de vinculo duplicado)
    if ($Apply) {
        try {
            Invoke-Gh -Arguments @('project', 'link', "$number", '--owner', $ProjectOwner, '--repo', $Repo) | Out-Null
            Write-Ok "project #$number vinculado ao repositório $FullRepo"
        }
        catch {
            Write-Skip "project #$number já vinculado ou sem permissão para novo vínculo"
        }
    }

    # campos
    $fields = Get-ProjectFields -Number $number
    $fieldNames = @($fields | ForEach-Object { $_.name })

    $wanted = @(
        @{ name = 'Prioridade'; type = 'SINGLE_SELECT'; options = @('Alta', 'Média', 'Baixa') },
        @{ name = 'Épico'; type = 'SINGLE_SELECT'; options = @($Data.epics | ForEach-Object { $_.option }) },
        @{ name = 'PB'; type = 'TEXT'; options = @() },
        @{ name = 'Tamanho'; type = 'SINGLE_SELECT'; options = @('P', 'M', 'G') }
    )

    foreach ($field in $wanted) {
        if ($fieldNames -contains $field.name) {
            Write-Skip "campo $($field.name) ja existe"
            continue
        }
        if ($Apply) {
            $arguments = @('project', 'field-create', "$number", '--owner', $ProjectOwner,
                '--name', $field.name, '--data-type', $field.type)
            if ($field.options.Count -gt 0) { $arguments += @('--single-select-options', ($field.options -join ',')) }
            Invoke-Gh -Arguments $arguments | Out-Null
            Write-Ok "campo $($field.name) criado"
        }
        else {
            Write-Plan "criar campo $($field.name) [$($field.type)]"
        }
    }

    # status: opcoes do campo built-in
    $fields = Get-ProjectFields -Number $number
    $statusField = $fields | Where-Object { $_.name -eq 'Status' } | Select-Object -First 1
    $statusOptions = @()
    if ($statusField) { $statusOptions = @($statusField.options | ForEach-Object { $_.name }) }
    if ($statusOptions -notcontains 'Backlog') {
        Write-Warn2 "Status do Project ainda está com as opções padrão. Ajuste na UI para: Backlog, Refinamento, Pronto, Em andamento, Em review, Concluído"
    }

    $targetStatusOptions = @('Backlog', 'Refinamento', 'Pronto', 'Em andamento', 'Em review', 'Concluído')
    if ($ConfigureStatus -and $statusField) {
        if (($statusOptions -join '|') -eq ($targetStatusOptions -join '|')) {
            Write-Skip 'opções do Status já configuradas'
            $statusOptions = $targetStatusOptions
        }
        elseif ($Apply) {
            $mutation = 'mutation($f:ID!){ updateProjectV2Field(input:{fieldId:$f, singleSelectOptions:[{name:"Backlog",color:GRAY,description:"Aguardando refinamento"},{name:"Refinamento",color:BLUE,description:"Em detalhamento"},{name:"Pronto",color:PURPLE,description:"Pronta para desenvolvimento"},{name:"Em andamento",color:YELLOW,description:"Em desenvolvimento"},{name:"Em review",color:ORANGE,description:"Aguardando revisão"},{name:"Concluído",color:GREEN,description:"Finalizada"}]}){ projectV2Field { ... on ProjectV2SingleSelectField { name options { name } } } } }'
            $res = Invoke-Graphql -Query $mutation -Variables @{ f = $statusField.id }
            Write-Ok 'opções do Status configuradas (Backlog, Refinamento, Pronto, Em andamento, Em review, Concluído)'
            $statusOptions = $targetStatusOptions
            $fields = Get-ProjectFields -Number $number
            $statusField = $fields | Where-Object { $_.name -eq 'Status' } | Select-Object -First 1
        }
        else {
            Write-Plan 'renomear as opções do Status para o fluxo do backlog'
        }
    }

    # itens
    $issues = Get-Issues
    $existingItems = Get-ProjectItems -Number $number
    $existingNumbers = @{}
    foreach ($item in $existingItems) {
        if ($item.content -and $item.content.number) { $existingNumbers[[int]$item.content.number] = $true }
    }

    $issueByMarker = @{}
    foreach ($issue in $issues) {
        $marker = Get-IssueMarker -Body $issue.body
        if ($marker) { $issueByMarker[$marker] = $issue }
    }

    $entries = @()
    foreach ($hu in $Data.hus) {
        $epic = $Data.epics | Where-Object { $_.id -eq $hu.epic } | Select-Object -First 1
        $entries += [pscustomobject]@{
            Marker     = $hu.id
            PB         = $hu.pb
            Priority   = $hu.priority
            EpicOption = $epic.option
        }
    }
    foreach ($epic in $Data.epics) {
        $entries += [pscustomobject]@{ Marker = $epic.id; PB = ''; Priority = 'Alta'; EpicOption = $epic.option }
    }

    foreach ($entry in $entries) {
        if (-not $issueByMarker.ContainsKey($entry.Marker)) {
            Write-Warn2 "$($entry.Marker) sem issue correspondente; item não adicionado"
            continue
        }
        $issue = $issueByMarker[$entry.Marker]
        $url = "https://github.com/$FullRepo/issues/$($issue.number)"
        $isNew = -not $existingNumbers.ContainsKey([int]$issue.number)

        if ($isNew) {
            if ($Apply) {
                Invoke-Gh -Arguments @('project', 'item-add', "$number", '--owner', $ProjectOwner, '--url', $url) | Out-Null
                Write-Ok "item #$($issue.number) adicionado ao project"
            }
            else { Write-Plan "adicionar #$($issue.number) ao project" }
        }
        else { Write-Skip "item #$($issue.number) ja esta no project" }

        if ($isNew -and $Apply) {
            if ($statusOptions -contains 'Backlog') {
                Invoke-Gh -Arguments @('project', 'item-edit', "$number", '--owner', $ProjectOwner, '--url', $url,
                    '--field', 'Status', '--value', 'Backlog') | Out-Null
            }
            Invoke-Gh -Arguments @('project', 'item-edit', "$number", '--owner', $ProjectOwner, '--url', $url,
                '--field', 'Prioridade', '--value', $entry.Priority) | Out-Null
            Invoke-Gh -Arguments @('project', 'item-edit', "$number", '--owner', $ProjectOwner, '--url', $url,
                '--field', 'Épico', '--value', $entry.EpicOption) | Out-Null
            if ($entry.PB) {
                Invoke-Gh -Arguments @('project', 'item-edit', "$number", '--owner', $ProjectOwner, '--url', $url,
                    '--field', 'PB', '--text', $entry.PB) | Out-Null
            }
        }
    }

    # normaliza status vazio ou legado (Todo/In Progress/Done) para o fluxo do backlog
    if ($Apply -and $statusOptions -contains 'Backlog') {
        $current = Get-ProjectItems -Number $number
        $backlogOption = $statusField.options | Where-Object { $_.name -eq 'Backlog' } | Select-Object -First 1
        if (-not $backlogOption) {
            Write-Warn2 'campo Status sem a opção Backlog; normalização ignorada (use -ConfigureStatus)'
        }
        $aliases = New-Object System.Collections.Generic.List[string]
        $index = 0
        if ($backlogOption) {
            foreach ($item in $current) {
                if (-not $item.id) { continue }
                if ($item.status -and $statusOptions -contains $item.status) { continue }
                $aliases.Add(("i{0}: updateProjectV2ItemFieldValue(input:{{projectId:`"{1}`", itemId:`"{2}`", fieldId:`"{3}`", value:{{singleSelectOptionId:`"{4}`"}}}}){{ projectV2Item {{ id }} }}" -f $index, $project.id, $item.id, $statusField.id, $backlogOption.id))
                $index++
            }
        }
        if ($aliases.Count -gt 0) {
            $mutation = 'mutation { ' + ($aliases -join ' ') + ' }'
            $res = Invoke-Graphql -Query $mutation
            Write-Ok "$($aliases.Count) itens movidos para o status Backlog"
        }
        else { Write-Skip 'status dos itens já está no fluxo do backlog' }
    }
}

# ---------------------------------------------------------------- indice

function Export-Index {
    Write-Step 'Índice do backlog'
    $issues = Get-Issues
    $byMarker = @{}
    foreach ($issue in $issues) {
        $marker = Get-IssueMarker -Body $issue.body
        if ($marker) { $byMarker[$marker] = $issue }
    }

    $project = $null
    if ($script:HasProjectScope) { $project = Get-Project }
    $projectUrl = ''
    if ($project) { $projectUrl = "https://github.com/users/$Owner/projects/$($project.number)" }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Índice do Backlog')
    $lines.Add('')
    $lines.Add('> Arquivo gerado por `scripts/sync-backlog.ps1 -ExportIndex`. Não editar manualmente.')
    $lines.Add('> A fonte viva do backlog é o GitHub Issues + Project.')
    $lines.Add('')
    if ($projectUrl) { $lines.Add("- Project: $projectUrl") }
    $lines.Add("- Milestone: $Milestone")
    $lines.Add('')
    $lines.Add('## Épicos')
    $lines.Add('')
    $lines.Add('| Épico | Issue |')
    $lines.Add('| --- | --- |')
    foreach ($epic in $Data.epics) {
        $issue = $byMarker[$epic.id]
        if ($issue) {
            $lines.Add("| $($epic.title) | [#$($issue.number)](https://github.com/$FullRepo/issues/$($issue.number)) |")
        }
        else {
            $lines.Add("| $($epic.title) | - |")
        }
    }
    $lines.Add('')
    $lines.Add('## Histórias de usuário')
    $lines.Add('')
    $lines.Add('| PB | HU | Título | Issue | Branch |')
    $lines.Add('| --- | --- | --- | --- | --- |')
    foreach ($hu in $Data.hus) {
        $issue = $byMarker[$hu.id]
        $issueCell = '-'
        if ($issue) { $issueCell = "[#$($issue.number)](https://github.com/$FullRepo/issues/$($issue.number))" }
        $lines.Add("| $($hu.pb) | $($hu.id) | $($hu.title) | $issueCell | ``$($hu.branch)`` |")
    }
    $lines.Add('')

    $path = Join-Path $Root '.github\backlog\INDEX.md'
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, ($lines -join "`n"), $encoding)
    Write-Ok "indice gerado em $path"
}

# ---------------------------------------------------------------- execucao

Write-Host ''
Write-Host "Backlog sync - $FullRepo (Apply=$Apply, Scope=$Scope)" -ForegroundColor White
Write-Host ''

if ($Scope -eq 'all' -or $Scope -eq 'labels') { Sync-Labels }
if ($Scope -eq 'all' -or $Scope -eq 'issues') {
    Sync-Milestone
    Sync-Issues
}
if ($Scope -eq 'all' -or $Scope -eq 'project') { Sync-Project }
if ($ExportIndex) {
    if ($Apply) { Export-Index }
    else { Write-Plan 'gerar .github/backlog/INDEX.md (requer -Apply)' }
}

Write-Host ''
if ($Apply) { Write-Ok 'Sincronizacao concluida.' }
else { Write-Warn2 'Dry-run: nenhuma alteracao foi feita. Use -Apply para aplicar.' }
