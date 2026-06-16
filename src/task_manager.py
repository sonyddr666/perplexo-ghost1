"""
Task Manager - Gerenciador de Tarefas Agendadas do Perplexo Bot

Responsável por:
1. Armazenar tarefas em JSON por usuário
2. Integrar com APScheduler para execução agendada
3. Executar buscas no horário e enviar notificações
"""

import json
import os
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any, Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Diretório de dados
TASKS_DIR = Path(__file__).parent.parent / "data" / "tasks"
TASKS_DIR.mkdir(parents=True, exist_ok=True)


class Task:
    """Representa uma tarefa agendada"""
    
    def __init__(
        self,
        name: str,
        prompt: str,
        schedule_type: str = "once",  # "once", "daily", "weekly"
        schedule_time: str = "09:00",  # HH:MM
        user_id: int = 0,
        task_id: str = None,
        model: str = "best",
        enabled: bool = True,
        created_at: str = None,
        last_run: str = None
    ):
        self.task_id = task_id or str(uuid.uuid4())[:8]
        self.name = name
        self.prompt = prompt
        self.schedule_type = schedule_type
        self.schedule_time = schedule_time
        self.user_id = user_id
        self.model = model
        self.enabled = enabled
        self.created_at = created_at or datetime.now().isoformat()
        self.last_run = last_run
    
    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "schedule_time": self.schedule_time,
            "user_id": self.user_id,
            "model": self.model,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run": self.last_run
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(**data)
    
    def __repr__(self):
        return f"Task({self.name}, {self.schedule_type}@{self.schedule_time})"


class TaskManager:
    """Gerencia tarefas por usuário"""
    
    def __init__(self, scheduler=None, execute_callback: Callable = None):
        """
        Args:
            scheduler: APScheduler instance (AsyncIOScheduler)
            execute_callback: Async function(user_id, task) -> None
        """
        self.scheduler = scheduler
        self.execute_callback = execute_callback
        self.pending_tasks: Dict[str, Task] = {}  # task_id -> Task (aguardando confirmação)
        logger.info(f"📋 TaskManager inicializado. Dir: {TASKS_DIR}")
    
    def _get_user_file(self, user_id: int) -> Path:
        return TASKS_DIR / f"{user_id}.json"
    
    def get_tasks(self, user_id: int) -> List[Task]:
        """Retorna todas as tarefas do usuário"""
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return [Task.from_dict(t) for t in data.get('tasks', [])]
        except Exception as e:
            logger.error(f"Erro ao ler tarefas de {user_id}: {e}")
            return []
    
    def save_task(self, task: Task) -> bool:
        """Salva ou atualiza uma tarefa"""
        file_path = self._get_user_file(task.user_id)
        
        # Lê existentes
        tasks = self.get_tasks(task.user_id)
        
        # Atualiza ou adiciona
        found = False
        for i, t in enumerate(tasks):
            if t.task_id == task.task_id:
                tasks[i] = task
                found = True
                break
        
        if not found:
            tasks.append(task)
        
        # Salva
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump({'tasks': [t.to_dict() for t in tasks]}, f, ensure_ascii=False, indent=2)
            
            # Agenda no scheduler
            if self.scheduler and task.enabled:
                self._schedule_task(task)
            
            logger.info(f"✅ Tarefa salva: {task}")
            return True
        except Exception as e:
            logger.error(f"Erro ao salvar tarefa: {e}")
            return False
    
    def delete_task(self, user_id: int, task_id: str) -> bool:
        """Remove uma tarefa"""
        tasks = self.get_tasks(user_id)
        original_len = len(tasks)
        tasks = [t for t in tasks if t.task_id != task_id]
        
        if len(tasks) == original_len:
            return False  # Não encontrou
        
        file_path = self._get_user_file(user_id)
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump({'tasks': [t.to_dict() for t in tasks]}, f, ensure_ascii=False, indent=2)
            
            # Remove do scheduler
            if self.scheduler:
                job_id = f"task_{user_id}_{task_id}"
                try:
                    self.scheduler.remove_job(job_id)
                except:
                    pass
            
            logger.info(f"🗑️ Tarefa removida: {task_id}")
            return True
        except Exception as e:
            logger.error(f"Erro ao deletar tarefa: {e}")
            return False
    
    def _schedule_task(self, task: Task):
        """Agenda uma tarefa no APScheduler"""
        if not self.scheduler:
            logger.warning("Scheduler não configurado")
            return
        
        job_id = f"task_{task.user_id}_{task.task_id}"
        
        # Remove job anterior se existir
        try:
            self.scheduler.remove_job(job_id)
        except:
            pass
        
        # Parse horário
        hour, minute = map(int, task.schedule_time.split(':'))
        
        if task.schedule_type == "once":
            # Roda uma vez (no próximo horário especificado)
            from apscheduler.triggers.date import DateTrigger
            from datetime import datetime, timedelta
            
            now = datetime.now()
            run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_time <= now:
                run_time += timedelta(days=1)
            
            self.scheduler.add_job(
                self._execute_wrapper,
                DateTrigger(run_date=run_time),
                args=[task.user_id, task.task_id],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"📅 Agendado (once): {task.name} para {run_time}")
            
        elif task.schedule_type == "daily":
            from apscheduler.triggers.cron import CronTrigger
            
            self.scheduler.add_job(
                self._execute_wrapper,
                CronTrigger(hour=hour, minute=minute),
                args=[task.user_id, task.task_id],
                id=job_id,
                replace_existing=True
            )
            logger.info(f"📅 Agendado (daily): {task.name} às {task.schedule_time}")
    
    async def _execute_wrapper(self, user_id: int, task_id: str):
        """Wrapper para executar tarefa agendada"""
        tasks = self.get_tasks(user_id)
        task = next((t for t in tasks if t.task_id == task_id), None)
        
        if not task or not task.enabled:
            logger.warning(f"Tarefa não encontrada ou desabilitada: {task_id}")
            return
        
        # Atualiza last_run
        task.last_run = datetime.now().isoformat()
        self.save_task(task)
        
        # Executa callback
        if self.execute_callback:
            try:
                await self.execute_callback(user_id, task)
            except Exception as e:
                logger.error(f"Erro ao executar tarefa {task_id}: {e}")
    
    def add_pending_task(self, task: Task) -> str:
        """Adiciona tarefa pendente (aguardando confirmação)"""
        self.pending_tasks[task.task_id] = task
        return task.task_id
    
    def confirm_pending_task(self, task_id: str) -> Optional[Task]:
        """Confirma e salva tarefa pendente"""
        task = self.pending_tasks.pop(task_id, None)
        if task:
            self.save_task(task)
        return task
    
    def cancel_pending_task(self, task_id: str) -> bool:
        """Cancela tarefa pendente"""
        return self.pending_tasks.pop(task_id, None) is not None
    
    def load_all_tasks(self):
        """Carrega e agenda todas as tarefas de todos os usuários (startup)"""
        if not self.scheduler:
            return
        
        for file_path in TASKS_DIR.glob("*.json"):
            try:
                user_id = int(file_path.stem)
                tasks = self.get_tasks(user_id)
                for task in tasks:
                    if task.enabled:
                        self._schedule_task(task)
            except Exception as e:
                logger.error(f"Erro ao carregar tarefas de {file_path}: {e}")
        
        logger.info(f"📋 Tarefas carregadas de {len(list(TASKS_DIR.glob('*.json')))} usuários")


# Singleton global (será inicializado pelo bot)
task_manager: Optional[TaskManager] = None


def init_task_manager(scheduler=None, execute_callback=None) -> TaskManager:
    """Inicializa o gerenciador global de tarefas"""
    global task_manager
    task_manager = TaskManager(scheduler=scheduler, execute_callback=execute_callback)
    task_manager.load_all_tasks()
    return task_manager


def get_task_manager() -> Optional[TaskManager]:
    """Retorna o gerenciador global"""
    return task_manager
