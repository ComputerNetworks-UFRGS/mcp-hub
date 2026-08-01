# Docs práticos
## Interface
### Profiles
Servem para salvar configurações dos agentes - estrutura, prompts e servidores MCP.
### Agent Structure
Define estrutura entre: 
- Agente único, com acesso a todos os MCPs;
- Agente Orquestrador, com um sub-agente por MCP;
- 'Magentic': Agente Orquestrador guiado por uma lista de tarefas (task e progress ledgers), com um sub-agente por MCP;
- 'Tool Call': Agente Orquestrador, com um sub-agente por MCP, chama por tool call em vez de JSON na resposta.

Sub-agent Memory: 
- Stateful: sub-agentes mantém memória das últimas vezes que foram chamados na conversa;
- Stateless: sub-agentes não mantém  memória entre chamadas.
### MCP Servers
Permite adicionar outras ferramentas para os agentes. 
URL deve ser pública ou interna ao cluster.
### Prompts
System Prompts passadas aos agentes a cada chamada.
### Credenciais
Permite passar credenciais privadas para os servidores MCP, salvando-as no seu browser em vez dos profiles.

## Administração
### Per-user provisioning

For each new user, three steps are required:

**1. Allow the k8s-mcp SA to impersonate this user** (add to the ClusterRole):
```bash
kubectl patch clusterrole k8s-mcp-impersonator --type=json \
  -p='[{"op":"add","path":"/rules/0/resourceNames/-","value":"<USERNAME>-readonly"}]'
```

**2. Grant view access in the user's namespace:**
```bash
kubectl create rolebinding <USERNAME>-k8s-mcp-readonly \
  --clusterrole=view \
  --user=<USERNAME>-readonly \
  -n <NAMESPACE>
```

**3. In Keycloak:** create the user and add them to the `k8s-agent-users` group.

`<USERNAME>` must match the user's Keycloak `preferred_username`. The `-readonly` suffix is appended by Agent Studio automatically. Username editing must be disabled in Keycloak realm settings so the name is stable.

