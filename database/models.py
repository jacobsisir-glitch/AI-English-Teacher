from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from database.database import Base


class Student(Base):
    __tablename__ = "students"

    student_id = Column(String(64), primary_key=True, index=True)
    session_summary = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class StudyLog(Base):
    __tablename__ = "study_logs"

    id = Column(Integer, primary_key=True, index=True)
    session_type = Column(String(50), nullable=False)
    start_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    duration_mins = Column(Integer, nullable=False)


class KnowledgeMastery(Base):
    __tablename__ = "knowledge_mastery"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(String(64), nullable=False, default="default_student", index=True)
    grammar_point = Column(String(100), index=True, nullable=False)
    mastery_score = Column(Integer, default=0, nullable=False)
    status = Column(String(20), default="learning", nullable=False)
    last_tested_at = Column(DateTime, nullable=True)


class ErrorBook(Base):
    __tablename__ = "error_book"

    id = Column(Integer, primary_key=True, index=True)
    grammar_point = Column(String(100), nullable=False)
    error_tag = Column(String(100), nullable=False)
    user_input = Column(Text, nullable=False)
    ai_comment = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class StudentQuestion(Base):
    __tablename__ = "student_questions"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(String(64), nullable=False, default="default_student", index=True)
    question_text = Column(Text, nullable=False)
    mode = Column(String(50), nullable=False, default="practice")
    source = Column(String(50), nullable=False, default="practice")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
