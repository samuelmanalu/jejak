-- Jejak — MySQL schema for the prompt log (idempotent).
CREATE DATABASE IF NOT EXISTS claude_logs
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE claude_logs;

CREATE TABLE IF NOT EXISTS prompt_logs (
  id           BIGINT NOT NULL AUTO_INCREMENT,
  session_id   VARCHAR(255)  NOT NULL,
  cwd          VARCHAR(1024) NOT NULL,
  prompt       TEXT          NOT NULL,
  tags         JSON          NOT NULL,
  machine_name VARCHAR(255)  NOT NULL,
  created_at   TIMESTAMP     NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_created_at   (created_at),
  KEY idx_session_id   (session_id),
  KEY idx_machine_name (machine_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
