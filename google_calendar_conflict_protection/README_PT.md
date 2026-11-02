# AdviceBiz — Google Calendar Conflict Protection — Odoo 19 Enterprise
## Versão 19.0.1.7.0 — candidata para testes em DEV

### Correções v1.7 — 15/10/2026

- **Erro `Invalid field 'gc_guard_remote_archived' in 'calendar.event'`:**
  declara agora o campo Boolean persistente que já era usado no código v1.6
  ao arquivar/restaurar reuniões canceladas no Google. A atualização do
  módulo regista a coluna na base de dados Odoo. O valor inicial é `False`.
- **Convites recusados:** a vista do calendário oculta um evento sincronizado
  quando a resposta do **próprio utilizador** é `declined`, mesmo quando o
  registo `calendar.event` tem esse utilizador em `user_id`. Isto é possível
  em convites Google com organizador externo. Não altera outros participantes.
- **Convites reaceites:** a atualização periódica ou o refresh ao clicar deve
  sincronizar a resposta `accepted` e voltar a mostrar o mesmo evento, mantendo
  a associação pelo `google_id`.
- **Sem alterações à cadência** (continuam os 5 minutos existentes), nem às
  regras de conflito e nem a convites/recorrências.

**Nota sobre dados anteriores:** esta correção resolve a ausência do campo,
mas não recupera automaticamente relações `google_id` apagadas por versões
anteriores, nem garante a resolução de conflitos já incorretos na base de
 dados. Testar a reunião problemática depois da atualização e guardar os logs
se persistir alguma divergência.



### Correção v1.6: resposta do convidado ao convite

Distingue explicitamente duas informações diferentes da API Google Calendar:
- **Estado do evento** `status=cancelled`: evento cancelado/eliminado.
- **Resposta do participante** `attendees[].responseStatus`: `accepted`,
  `declined`, `tentative`, `needsAction`.

**Quando o Odoo pertence ao convidado e não ao organizador:**

1. Aceite no Google -> `calendar.attendee.state=accepted` no Odoo.
2. Recusado no Google -> `calendar.attendee.state=declined`. No calendário do
   convidado, o evento **é ocultado apenas na apresentação**, sem ser arquivado
   nem perder o `google_id`.
3. Aceite novamente no Google -> `state=accepted` e o mesmo evento volta a
   aparecer após a próxima sincronização (5 minutos na configuração atual).
4. Provisoriamente aceite -> `tentative`.
5. Cancelado pelo organizador (`status=cancelled`) -> o evento é arquivado no
   Odoo e permanece ligado ao `google_id` para permitir uma eventual
   recuperação, sem enviar um DELETE à API Google.
6. Se o Google restaurar um evento anteriormente arquivado **por cancelamento
   remoto nesta versão**, o mesmo registo `calendar.event` é reativado e
   atualizado a partir do Google. Não é criado um evento duplicado.

**Correção técnica:** o estado do convidado é sincronizado *independentemente*
da comparação de datas `calendar.event.write_date` e do ETag do evento. Só se
atualiza o participante da própria conta Google autenticada. O estado de outros
convidados não é alterado por esta proteção. Uma falta de correspondente Odoo
para o próprio convidado é resolvida criando esse participante no evento.

Ocultar convites recusados é uma personalização da vista Calendário do Odoo;
o evento mantém-se na base de dados e pode continuar visível em outras vistas
ou relatórios de administração. Isso permite recuperar a aceitação sem perder
histórico, seguidores, documentação ou dados de Marcações.

### Atenção a reuniões arquivadas pela v1.5

Na v1.5, o tratamento de alguns cancelamentos removia o campo `google_id`.
**Estes registos antigos não são ligados automaticamente na atualização para
v1.6**: restaurar um vínculo exige identificar e validar o ID Google correto.
Não tentar ligar reuniões pelo assunto e horário, pois isso pode unir eventos
não relacionados. Guardar uma cópia da BD antes da atualização.

### Teste prioritário: Aceite -> Recusado -> Aceite

1. Utilizar duas contas distintas, com uma como organizador no Google e outra
   como convidado, que tem sincronização ativa com o Odoo.
2. Criar o convite no Google como organizador e aceitar com o convidado.
3. Aguardar até 5 minutos: o evento deve aparecer normalmente no Odoo.
4. Recusar **apenas a participação** com a conta convidada no Google.
5. Aguardar uma sincronização e recarregar o Calendário do Odoo: a reunião deve
   desaparecer apenas dessa vista da conta convidada, sem ser eliminada.
6. Aceitar novamente no Google com a mesma conta convidada.
7. Aguardar nova sincronização e recarregar: o evento deve reaparecer **sem
   risco no título** e com `calendar.attendee.state=accepted`.
8. Verificar que o Google não recebeu um convite, cancelamento ou alteração
   de horário enviado pelo Odoo durante os passos anteriores.
9. Repetir com estado provisório, evento de Marcações e com conta Odoo normal.
10. Separadamente, testar cancelamento do evento pelo organizador. Este deve
    arquivar a reunião, sem interferir com os outros convidados.

### Funcionalidades anteriores mantidas

- Consulta do evento Google ao clicar, antes de abrir a reunião no Odoo;
  nova validação antes de editar, guardar, eliminar ou arrastar.
- Sincronização bidirecional, preservando o cron existente de 5 minutos.
- Conflitos de sincronização do mesmo evento: versões Odoo e Google preservadas
  e decisão manual, sem criar uma reunião real duplicada.
- Sobreposição de horários de eventos distintos: aviso sem fusão automática.
- Atualização condicional `If-Match` para reduzir sobrescritas concorrentes.
- Limitação de edição de eventos recorrentes da versão anterior.

### Instalação / atualização

1. Fazer backup da base de dados e dos módulos instalados no DEV.
2. Extrair a pasta `google_calendar_conflict_protection` para `custom_addons`.
3. Reiniciar o Odoo, atualizar a lista de aplicações e atualizar o módulo.
4. Recarregar o browser com `Ctrl+Shift+R` para obter os novos assets JS.
5. Testar o ciclo de respostas acima, uma vez a aguardar o cron e outra com
   sincronização manual. Só depois avaliar instalação em PRE.

**Estado de validação:** validada apenas a sintaxe Python/JavaScript e estrutura
XML/ZIP; não foi testado num servidor real Odoo 19 Enterprise nem com as
credenciais Google do cliente. Testes com eventos recorrentes, convites de
terceiros e permissões normais são obrigatórios antes de produção.
