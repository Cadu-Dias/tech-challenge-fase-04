package main

import (
	"context"
	"database/sql"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "github.com/jackc/pgx/v4/stdlib"
	"github.com/joho/godotenv"
)

// App struct (para injeção de dependência)
type App struct {
	DB           *sql.DB
	MasterKey    string
	FaultEnabled bool
	FaultMarker  string
}

func main() {
	// Carrega o .env para desenvolvimento local. Em produção, isso não fará nada.
	_ = godotenv.Load()
	faultEnabled := os.Getenv("AUTH_FAULT_INJECTION_ENABLED") == "true"
	if len(os.Args) > 1 {
		if len(os.Args) != 2 || os.Args[1] != "inject-failure" {
			log.Fatal("Argumento desconhecido")
		}
		if err := injectFault(faultEnabled, faultMarkerPath); err != nil {
			log.Fatalf("Falha ao injetar erro controlado: %v", err)
		}
		log.Print("Falha controlada ativada neste contêiner")
		return
	}
	shutdownTelemetry, err := initTelemetry("auth-service")
	if err != nil {
		log.Fatalf("Falha ao iniciar telemetria: %v", err)
	}
	defer shutdownTelemetry()

	// --- Configuração ---
	port := os.Getenv("PORT")
	if port == "" {
		port = "8001" // Porta padrão
	}

	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		log.Fatal("DATABASE_URL deve ser definida")
	}

	masterKey := os.Getenv("MASTER_KEY")
	if masterKey == "" {
		log.Fatal("MASTER_KEY deve ser definida")
	}

	// --- Conexão com o Banco ---
	db, err := connectDB(databaseURL)
	if err != nil {
		log.Fatalf("Não foi possível conectar ao banco de dados: %v", err)
	}
	defer db.Close()

	app := &App{
		DB:           db,
		MasterKey:    masterKey,
		FaultEnabled: faultEnabled,
		FaultMarker:  faultMarkerPath,
	}

	// --- Rotas da API ---
	mux := http.NewServeMux()
	mux.HandleFunc("/health", app.healthHandler)

	// Endpoint público para validar uma chave
	mux.Handle("/validate", traceHandler("/validate", http.HandlerFunc(app.validateKeyHandler)))

	// Endpoints de "admin" para criar/gerenciar chaves
	// Estas rotas são protegidas pelo middleware de autenticação
	mux.Handle("/admin/keys", traceHandler("/admin/keys", app.masterKeyAuthMiddleware(http.HandlerFunc(app.createKeyHandler))))

	log.Printf("Serviço de Autenticação (Go) rodando na porta %s", port)
	server := &http.Server{Addr: ":" + port, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	ctxSignal, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		<-ctxSignal.Done()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := server.Shutdown(ctx); err != nil {
			log.Printf("Erro ao encerrar servidor HTTP: %v", err)
		}
	}()
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		shutdownTelemetry()
		log.Fatalf("Erro no servidor HTTP: %v", err)
	}
	<-shutdownDone
}

// connectDB inicializa e testa a conexão com o PostgreSQL
func connectDB(databaseURL string) (*sql.DB, error) {
	db, err := sql.Open("pgx", databaseURL)
	if err != nil {
		return nil, err
	}

	if err = db.Ping(); err != nil {
		return nil, err
	}

	log.Println("Conectado ao PostgreSQL com sucesso!")
	return db, nil
}
