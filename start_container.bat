@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  Automation Studio container launcher
REM
REM  Usage:
REM    start_container.bat            Smart start. Starts Docker Desktop if needed,
REM                                   builds the image if missing, creates or starts
REM                                   the container, waits until it is healthy and
REM                                   opens the dashboard in your browser.
REM    start_container.bat build      Force-rebuild the image (re-creates container).
REM    start_container.bat open       Open the dashboard.
REM    start_container.bat shell      Open a shell inside the running container.
REM    start_container.bat stop       Stop the container (keep it for fast restart).
REM    start_container.bat remove     Stop AND remove the container.
REM    start_container.bat logs       Follow container logs (Ctrl+C exits).
REM    start_container.bat status     Show container state.
REM ============================================================

REM -------- SETTINGS --------
set "IMAGE=automation-studio:latest"
set "CONTAINER_NAME=automation-studio"
set "HOST_PORT=8010"
set "VNC_PORT=6080"
set "WORKDIR=/app"
set "DASHBOARD_URL=http://localhost:%HOST_PORT%"
set "LIVE_VIEW_URL=http://localhost:%VNC_PORT%/vnc.html?autoconnect=true&resize=scale&reconnect=true"

REM Run from the directory this .bat lives in, regardless of where it's invoked from.
cd /d "%~dp0"
set "HOST_PATH=%cd%"
REM C:\Projects\X -> C:/Projects/X for the Docker Desktop bind mount.
set "DOCKER_PATH=%HOST_PATH:\=/%"

REM -------- Dispatch on first arg --------
set "ACTION=%~1"
if /I "%ACTION%"=="/?"      goto :usage
if /I "%ACTION%"=="-h"      goto :usage
if /I "%ACTION%"=="--help"  goto :usage
if /I "%ACTION%"=="help"    goto :usage
if /I "%ACTION%"=="build"   goto :build
if /I "%ACTION%"=="rebuild" goto :build
if /I "%ACTION%"=="open"    goto :open
if /I "%ACTION%"=="shell"   goto :shell
if /I "%ACTION%"=="stop"    goto :stop
if /I "%ACTION%"=="remove"  goto :remove
if /I "%ACTION%"=="down"    goto :remove
if /I "%ACTION%"=="logs"    goto :logs
if /I "%ACTION%"=="status"  goto :status
if /I "%ACTION%"=="ps"      goto :status
if not "%ACTION%"=="" (
    echo Unknown action: %ACTION%
    echo.
    goto :usage
)

REM -------- Default flow --------
echo ============================================================
echo  Automation Studio container launcher
echo    image      : %IMAGE%
echo    container  : %CONTAINER_NAME%
echo    mount      : %DOCKER_PATH%  ^-^>  %WORKDIR%
echo    dashboard  : %DASHBOARD_URL%
echo    live view  : http://localhost:%VNC_PORT%
echo ============================================================

call :ensure_docker || goto :fail

REM Build image if missing.
docker image inspect %IMAGE% >NUL 2>&1
if errorlevel 1 (
    echo Image %IMAGE% not found. Building. The first build takes a few minutes...
    docker build -t %IMAGE% .
    if errorlevel 1 (
        echo [start_container] image build failed.
        goto :fail
    )
) else (
    echo Image %IMAGE% present. Skipping build. Use "start_container.bat build" to force rebuild.
)

REM Does a container with this name already exist?
docker ps -a -q -f "name=^%CONTAINER_NAME%$" | findstr . >NUL
if !ERRORLEVEL! equ 0 (
    for /f "delims=" %%i in ('docker inspect -f "{{.State.Status}}" %CONTAINER_NAME%') do set "STATUS=%%i"
    if /I "!STATUS!"=="running" (
        echo Container %CONTAINER_NAME% is already running.
    ) else (
        echo Container %CONTAINER_NAME% exists but is !STATUS!. Starting...
        docker start %CONTAINER_NAME% >NUL
        if errorlevel 1 (
            echo [start_container] docker start failed.
            goto :fail
        )
    )
) else (
    call :create_container || goto :fail
)

call :wait_healthy || goto :fail
goto :open

:build
call :ensure_docker || goto :fail
echo Force-rebuilding %IMAGE%...
docker build -t %IMAGE% .
if errorlevel 1 (
    echo [start_container] image build failed.
    goto :fail
)
REM Re-create the container so it picks up the fresh image.
docker rm -f %CONTAINER_NAME% >NUL 2>&1
call :create_container || goto :fail
call :wait_healthy || goto :fail
goto :open

:create_container
echo Creating new container %CONTAINER_NAME%...
REM Give the container this PC's time zone (as a POSIX offset such as LOCAL-05:30)
REM so dashboard times and {now} in test data match your clock.
set "TZ_POSIX=UTC"
for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "$o=[TimeZoneInfo]::Local.GetUtcOffset([DateTime]::Now); $sign='-'; if ($o.Ticks -lt 0) { $sign='+' }; 'LOCAL{0}{1:00}:{2:00}' -f $sign, [Math]::Abs($o.Hours), [Math]::Abs($o.Minutes)"`) do set "TZ_POSIX=%%t"
REM Ports are published on 127.0.0.1 only: the live view has no password, so
REM it must not be reachable from other machines.
docker run -d ^
    --name %CONTAINER_NAME% ^
    -e "STUDIO_LIVE_VIEW_URL=%LIVE_VIEW_URL%" ^
    -e "TZ=%TZ_POSIX%" ^
    -v "%DOCKER_PATH%:%WORKDIR%" ^
    -w %WORKDIR% ^
    -p 127.0.0.1:%HOST_PORT%:8010 ^
    -p 127.0.0.1:%VNC_PORT%:6080 ^
    --shm-size=1g ^
    --restart unless-stopped ^
    %IMAGE% >NUL
if errorlevel 1 (
    echo [start_container] docker run failed. Is port %HOST_PORT% or %VNC_PORT% already in use?
    echo Change HOST_PORT / VNC_PORT at the top of this file if another app needs them.
    docker rm -f %CONTAINER_NAME% >NUL 2>&1
    exit /b 1
)
exit /b 0

:wait_healthy
echo Waiting for Automation Studio to start...
for /l %%n in (1,1,60) do (
    curl.exe -s -f -o NUL "%DASHBOARD_URL%/api/health" >NUL 2>&1
    if !ERRORLEVEL! equ 0 (
        echo Automation Studio is up.
        exit /b 0
    )
    ping -n 2 127.0.0.1 >NUL
)
echo [start_container] the app did not respond within 60 seconds. Recent logs:
docker logs --tail 30 %CONTAINER_NAME%
exit /b 1

:ensure_docker
docker info >NUL 2>&1
if not errorlevel 1 exit /b 0
echo Docker is not running. Starting Docker Desktop...
set "DD_EXE="
if exist "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe" set "DD_EXE=%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" set "DD_EXE=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
if not defined DD_EXE (
    echo [start_container] Docker Desktop was not found. Install it or start Docker manually.
    exit /b 1
)
start "" "%DD_EXE%"
for /l %%n in (1,1,90) do (
    docker info >NUL 2>&1
    if !ERRORLEVEL! equ 0 (
        echo Docker is ready.
        exit /b 0
    )
    ping -n 3 127.0.0.1 >NUL
)
echo [start_container] Docker did not become ready in time.
exit /b 1

:open
echo.
echo  Dashboard : %DASHBOARD_URL%
echo  Live view : shown inside the dashboard while signing in, recording or running
echo.
echo  Stop later with: start_container.bat stop    Logs: start_container.bat logs
start "" "%DASHBOARD_URL%"
goto :end

:shell
docker ps -q -f "name=^%CONTAINER_NAME%$" | findstr . >NUL
if errorlevel 1 (
    echo Container %CONTAINER_NAME% is not running. Use "start_container.bat" first.
    exit /b 1
)
docker exec -it %CONTAINER_NAME% bash -lc "cd %WORKDIR% && exec bash"
goto :end

:stop
echo Stopping %CONTAINER_NAME%...
docker stop %CONTAINER_NAME%
goto :end

:remove
echo Stopping and removing %CONTAINER_NAME%...
docker rm -f %CONTAINER_NAME%
goto :end

:logs
echo Tailing %CONTAINER_NAME% logs. Press Ctrl+C to stop.
docker logs -f %CONTAINER_NAME%
goto :end

:status
docker ps -a -f "name=^%CONTAINER_NAME%$"
goto :end

:usage
echo Usage:
echo   start_container.bat           Smart start (Docker, image, container), then open the dashboard.
echo   start_container.bat build     Force-rebuild the image and re-create the container.
echo   start_container.bat open      Open the dashboard.
echo   start_container.bat shell     Open a shell inside the running container.
echo   start_container.bat stop      Stop the container (keep it for fast restart).
echo   start_container.bat remove    Stop AND remove the container.
echo   start_container.bat logs      Follow container logs.
echo   start_container.bat status    Show container state.
goto :end

:fail
echo.
pause
endlocal
exit /b 1

:end
endlocal
