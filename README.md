# dash_vps

Dashboard web em tempo real para monitorar VPS via SSH. Apenas biblioteca padrão do Python, sem dependências, sem agente instalado nos servidores.

Para cada servidor, a cada 2 segundos, ele conecta por SSH, lê `/proc` (somente leitura) e mostra no navegador:

- CPU, RAM e disco
- Upload/download em tempo real e totais
- Uptime, processos, conexões e load average

## Uso

Requisitos: Python 3 e cliente `ssh` no PATH.

1. Copie `nodes.example.json` para `nodes.json` e preencha com seus servidores (nome, ip, porta, usuário e caminho da chave privada). O `nodes.json` está no `.gitignore`.
2. Rode:

```bash
python monitor.py
```

3. Abra http://localhost:8080
